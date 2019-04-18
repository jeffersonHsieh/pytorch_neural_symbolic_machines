"""
BERT relation prediction

Usage:
    relation_predictor.py train [options] CONFIG_FILE
    relation_predictor.py test [options] MODEL_PATH TEST_FILE_PATH

Options:
    -h --help                                   Show this screen
    --no-cuda                                   Do not use GPU
    --debug                                     Debug mode
    --seed=<int>                                Seed [default: 0]
    --work-dir=<dir>                            work dir [default: exp_runs/debug]
    --extra-config=<str>                        extra config [default: {}]
"""

import math
import os
import sys
from collections import defaultdict

import torch
import random
import numpy as np
import json
from tqdm import tqdm
from docopt import docopt

from pytorch_pretrained_bert import BertForTokenClassification, BertTokenizer, BertAdam, PYTORCH_PRETRAINED_BERT_CACHE, \
    BertConfig

CONFIG_NAME = 'bert_config.json'
WEIGHTS_NAME = 'pytorch_model.bin'
MAX_SEQUENCE_LEN = 512
label_space = {'O': 0, 'I-COLUMN': 1}


def get_examples_eval_results(examples, predictions, target_labels, column_spans, verbose=False):
    correct_list = []
    col_wise_correct_list = []

    with torch.no_grad():
        for i, example in enumerate(examples):
            if verbose:
                print(f'Example: {example.question}', file=sys.stderr)

            example_column_span = column_spans[i]
            example_pred_labels = predictions[i].cpu().numpy()
            example_tgt_labels = target_labels[i].cpu().numpy()
            e_correct_list = []

            for col_id, column in enumerate(example.columns):
                col_span_start, col_span_end = example_column_span[column.name]
                if col_span_start > len(example_pred_labels):
                    is_triggered = False
                else:
                    column_pred_labels = example_pred_labels[col_span_start: col_span_end]
                    span_len = len(column_pred_labels)
                    is_triggered = (column_pred_labels == label_space['I-COLUMN']).sum() >= 0.8 * span_len

                is_correct = is_triggered and (col_id in example.target_column_ids) or \
                             not is_triggered and (col_id not in example.target_column_ids)

                e_correct_list.append(is_correct)

                if verbose:
                    print(f'>>Column: {column.name} [pred={is_triggered}, gold={col_id in example.target_column_ids}]', file=sys.stderr)

            is_example_correct = all(e_correct_list)
            correct_list.append(is_example_correct)
            col_wise_correct_list.extend(e_correct_list)
            if verbose:
                print(f'Example Correct: {is_example_correct}', file=sys.stderr)
                print('', file=sys.stderr)

    return {'column_wise_acc': col_wise_correct_list,
            'example_acc': correct_list}


def batch_iter(data, batch_size, shuffle=False):
    batch_num = math.ceil(len(data) / batch_size)
    index_array = list(range(len(data)))

    if shuffle:
        np.random.shuffle(index_array)

    for i in range(batch_num):
        indices = index_array[i * batch_size: (i + 1) * batch_size]
        examples = [data[idx] for idx in indices]

        yield examples


class Column(object):
    def __init__(self, name, type, sample_value=None, **kwargs):
        self.name = name
        self.type = type
        self.sample_value = sample_value

        for key, val in kwargs.items():
            setattr(self, key, val)


class Example(object):
    def __init__(self, question, columns, targets, **kwargs):
        self.question = question
        self.columns = columns
        self.target_column_ids = targets

        for key, val in kwargs.items():
            setattr(self, key, val)

    @classmethod
    def from_dict(cls, data, tokenizer):
        targets = []
        columns = []

        question = data['question']
        question_tokens = tokenizer.tokenize(question)

        column_start_id = 1 + len(question_tokens) + 1  # [CLS] + question + [SEP]
        for col_id, col_data in enumerate(data['columns']):
            if col_data['is_target']:
                targets.append(col_id)
            column = Column(col_data['name'], col_data['type'], col_data['sample_value'],
                            name_tokens=tokenizer.tokenize(col_data['name']),
                            type_tokens=tokenizer.tokenize(col_data['type']),
                            sample_value_tokens=tokenizer.tokenize(str(col_data['sample_value'])))
            columns.append(column)

        return cls(data['question'], columns, targets, question_tokens=question_tokens)

    @classmethod
    def to_tensor_dict(cls, examples, tokenizer, use_sample_value=False):
        all_tokens_ids = []
        all_label_ids = []
        all_segment_ids = []
        meta = {'column_spans': []}
        for example in examples:
            tokens = ['[CLS]'] + example.question_tokens + ['[SEP]']
            labels = ['O'] * len(tokens)
            segment_ids = [0] * len(tokens)

            col_spans = dict()
            col_start_idx = len(tokens)
            for col_id, column in enumerate(example.columns):
                col_tokens = column.name_tokens
                if use_sample_value:
                    col_tokens += ['('] + column.sample_value_tokens[:7] + [','] + column.type_tokens + [')']
                else:
                    col_tokens += ['('] + column.type_tokens + [')']
                # column_tokens.extend(col_tokens)

                column_label = 'I-COLUMN' if col_id in example.target_column_ids else 'O'
                col_labels = [column_label] * len(col_tokens)

                col_tokens.append('.')
                col_labels.append('O')

                col_spans[column.name] = (col_start_idx, col_start_idx + len(col_tokens))
                tokens.extend(col_tokens)
                labels.extend(col_labels)
                col_start_idx += len(col_tokens)

            tokens.append('[SEP]')
            labels.append('O')

            segment_ids += [1] * (len(tokens) - len(segment_ids))

            token_ids = tokenizer.convert_tokens_to_ids(tokens)
            label_ids = [label_space[x] for x in labels]
            assert len(tokens) == len(labels)

            all_tokens_ids.append(token_ids)
            all_label_ids.append(label_ids)
            all_segment_ids.append(segment_ids)
            meta['column_spans'].append(col_spans)

        max_len = min(max(len(x) for x in all_tokens_ids), MAX_SEQUENCE_LEN)

        input_ids = np.zeros((len(examples), max_len), dtype=np.int64)
        label_ids = np.zeros((len(examples), max_len), dtype=np.int64)
        input_mask = torch.zeros(len(examples), max_len)
        segment_ids = np.zeros((len(examples), max_len), dtype=np.int64)
        for i, token_ids in enumerate(all_tokens_ids):
            token_seq_len = min(len(token_ids), MAX_SEQUENCE_LEN)
            input_ids[i, :token_seq_len] = token_ids[:token_seq_len]
            input_mask[i, :token_seq_len] = 1.
            label_ids[i, :token_seq_len] = all_label_ids[i][:token_seq_len]
            segment_ids[i, :token_seq_len] = all_segment_ids[i][:token_seq_len]

        return (torch.from_numpy(input_ids), input_mask, torch.from_numpy(segment_ids), torch.from_numpy(label_ids)), meta


def load_dataset(file_path, tokenizer):
    examples = []
    with open(file_path) as f:
        for line in tqdm(f, desc=f'reading {file_path}', file=sys.stdout):
            json_dict = json.loads(line)
            example = Example.from_dict(json_dict, tokenizer)
            examples.append(example)

    return examples


def evaluate(model, dataset, tokenizer, device, batch_size, config, verbose=False):
    was_training = model.training
    model.eval()

    eval_loss = 0.
    num_steps = 0
    eval_result = defaultdict(list)

    for eval_iter, examples in enumerate(tqdm(batch_iter(dataset, batch_size, shuffle=False), total=len(dataset) // batch_size, file=sys.stdout)):
        batch, batch_mata = Example.to_tensor_dict(examples, tokenizer,
                                                   use_sample_value=config['use_sample_value'])

        batch = tuple(t.to(device) for t in batch)
        input_ids, input_mask, segment_ids, label_ids = batch

        with torch.no_grad():
            tmp_eval_loss = model(input_ids, segment_ids, input_mask, label_ids)
            tmp_eval_loss = tmp_eval_loss.sum() / input_mask.sum()

            logits = model(input_ids, segment_ids, input_mask)
            pred_label_ids = torch.argmax(logits, dim=-1)
            tmp_eval_result = get_examples_eval_results(examples, pred_label_ids, label_ids, batch_mata['column_spans'], verbose=verbose)
            for key, val in tmp_eval_result.items():
                eval_result[key].extend(val)

        eval_loss += tmp_eval_loss.item()
        num_steps += 1

    for key, val in eval_result.items():
        eval_result[key] = np.average(val)

    if was_training:
        model = model.train()

    return eval_result


def train(args):
    config = json.load(open(args['CONFIG_FILE']))
    #'--data-path': '/Users/yinpengcheng/Research/SemanticParsing/WikiSQL/annotated/dev.rel_prediction.small.jsonl',
    #'--dev-data-path': '/Users/yinpengcheng/Research/SemanticParsing/WikiSQL/annotated/dev.rel_prediction.small.jsonl'}

    work_dir = args['--work-dir']
    seed = int(args['--seed'])
    bert_model = config['bert_model']
    learning_rate = config['lr']
    num_train_epochs = config['train_epochs']
    warmup_proportion = config['warmup_proportion']
    gradient_accumulation_steps = config['gradient_accumulation_steps']
    batch_size = config['batch_size']
    batch_size = batch_size // gradient_accumulation_steps
    fix_bert = config['fix_bert']
    config['work-dir'] = work_dir

    tokenizer = BertTokenizer.from_pretrained(bert_model, do_lower_case=True)
    device = torch.device("cuda" if torch.cuda.is_available() and not args['--no-cuda'] else "cpu")
    n_gpu = torch.cuda.device_count()

    tr_loss = 0.
    nb_tr_examples = 0
    nb_tr_steps = 0

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    if not os.path.exists(work_dir):
        print(f'creating work dir [{work_dir}]', file=sys.stderr)
        os.makedirs(work_dir)

    if args['--extra-config'] != '{}':
        extra_config = args['--extra-config']
        extra_config = json.loads(extra_config)
        config.update(extra_config)

    json.dump(config, open(os.path.join(work_dir, 'config.json'), 'w'), indent=2)

    cache_dir = PYTORCH_PRETRAINED_BERT_CACHE
    model = BertForTokenClassification.from_pretrained(bert_model, cache_dir=cache_dir, num_labels=len(label_space))
    model = model.to(device)
    model = torch.nn.DataParallel(model)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    if fix_bert:
        for param_name, param in param_optimizer:
            if 'bert.' in param_name:
                param.requires_grad = False
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay) and p.requires_grad], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay) and p.requires_grad], 'weight_decay': 0.0}
    ]

    train_data = load_dataset(config['data']['train_file'], tokenizer)
    dev_data = load_dataset(config['data']['dev_file'], tokenizer)
    num_train_optimization_steps = math.ceil(len(train_data) / batch_size / gradient_accumulation_steps) * num_train_epochs

    optimizer = BertAdam(optimizer_grouped_parameters,
                         lr=learning_rate,
                         warmup=warmup_proportion,
                         t_total=num_train_optimization_steps)

    model.train()
    for epoch_id in range(num_train_epochs):
        for train_iter, examples in enumerate(tqdm(batch_iter(train_data, batch_size, shuffle=True), total=len(train_data) // batch_size, file=sys.stdout)):
            batch, batch_mata = Example.to_tensor_dict(examples, tokenizer, use_sample_value=config['use_sample_value'])

            batch = tuple(t.to(device) for t in batch)
            input_ids, input_mask, segment_ids, label_ids = batch
            loss = model(input_ids, segment_ids, input_mask, label_ids)
            if n_gpu > 1:
                loss = loss.mean()

            if gradient_accumulation_steps > 1:
                loss = loss / gradient_accumulation_steps

            loss.backward()

            iter_loss = loss.item()
            print(iter_loss, file=sys.stderr)
            tr_loss += iter_loss
            nb_tr_examples += input_ids.size(0)
            nb_tr_steps += 1

            del loss

            if (train_iter + 1) % gradient_accumulation_steps == 0:
                optimizer.step()
                optimizer.zero_grad()

        epoch_eval_result = evaluate(model, dev_data, tokenizer, device, batch_size, config, verbose=False)
        print(epoch_eval_result, file=sys.stderr)

        # Save a trained model and the associated configuration
        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
        output_model_file = os.path.join(work_dir, WEIGHTS_NAME)
        torch.save(model_to_save.state_dict(), output_model_file)
        output_config_file = os.path.join(work_dir, CONFIG_NAME)
        with open(output_config_file, 'w') as f:
            f.write(model_to_save.config.to_json_string())


def test(args):
    model_path = args['MODEL_PATH']
    work_dir = '/'.join(model_path.split('/')[:-1])
    config_file = os.path.join(work_dir, 'config.json')
    print(f'Model config file: {config_file}', file=sys.stderr)
    config = json.load(open(config_file))

    if args['--extra-config'] != '{}':
        extra_config = args['--extra-config']
        extra_config = json.loads(extra_config)
        print(f'load extra config: {extra_config}', file=sys.stderr)
        config.update(extra_config)

    # Load a trained model and config that you have fine-tuned
    output_config_file = os.path.join(work_dir, CONFIG_NAME)
    print(f'BERT config file: {output_config_file}', file=sys.stderr)
    bert_config = BertConfig(output_config_file)
    model = BertForTokenClassification(bert_config, num_labels=len(label_space))
    print(f'model file: {model_path}', file=sys.stderr)
    model.load_state_dict(torch.load(model_path))

    device = torch.device("cuda" if torch.cuda.is_available() and not args['--no-cuda'] else "cpu")

    model = model.to(device)
    model.eval()

    tokenizer = BertTokenizer.from_pretrained(config['bert_model'], do_lower_case=True)
    dev_data = load_dataset(args['TEST_FILE_PATH'], tokenizer)

    epoch_eval_result = evaluate(model, dev_data, tokenizer, device, config['batch_size'], config, verbose=True)

    print(epoch_eval_result, file=sys.stderr)


if __name__ == '__main__':
    cmd_args = docopt(__doc__)
    if cmd_args['train']:
        train(cmd_args)
    elif cmd_args['test']:
        test(cmd_args)
