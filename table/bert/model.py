import sys
from argparse import Namespace
from collections import OrderedDict
from pathlib import Path
import json
from types import SimpleNamespace
from typing import List, Dict, Union, Optional
import numpy as np

import torch
from pytorch_pretrained_bert import BertTokenizer
from torch_scatter import scatter_max, scatter_mean
import torch.nn as nn

from pytorch_pretrained_bert.modeling import BertPreTrainedModel, BertModel, BertConfig, BertForMaskedLM, \
    BertForPreTraining
from torch.nn import CrossEntropyLoss, BCEWithLogitsLoss

from table.bert.data_model import Example, Table, Column

NEGATIVE_NUMBER = -1e8
MAX_BERT_INPUT_LENGTH = 512
CONFIG_NAME = 'bert_config.json'
WEIGHTS_NAME = 'pytorch_model.bin'


def get_column_representation(flattened_column_encoding: torch.Tensor,
                              column_token_to_column_id: torch.Tensor,
                              column_token_mask: torch.Tensor,
                              column_mask: torch.Tensor,
                              aggregator: str = 'mean_pool') -> torch.Tensor:
    """
    Args:
        flattened_column_encoding: (batch_size, total_column_token_num, encoding_size)
        column_token_to_column_id: (batch_size, total_column_token_num + 1)
        column_mask: (batch_size, max_column_num)

    Returns:
        column_encoding: (batch_size, max_column_num, encoding_size)
    """

    if aggregator.startswith('max_pool'):
        agg_func = scatter_max
        flattened_column_encoding[column_token_mask == 0] = float('-inf')
    elif aggregator.startswith('mean_pool') or aggregator.startswith('first_token'):
        agg_func = scatter_mean
    else:
        raise ValueError(f'Unknown column representation method {aggregator}')

    max_column_num = column_mask.size(-1)
    # column_token_to_column_id: (batch_size, max_column_num)
    # (batch_size, max_column_size + 1, encoding_size)
    result = agg_func(flattened_column_encoding,
                      column_token_to_column_id.unsqueeze(-1).expand(-1, -1, flattened_column_encoding.size(-1)),
                      dim=1,
                      dim_size=max_column_num + 1)

    # remove the last "garbage collection" entry, mask out padding columns
    result = result[:, :-1] * column_mask.unsqueeze(-1)

    if aggregator == 'max_pool':
        column_encoding = result[0]
    else:
        column_encoding = result

    return column_encoding


class TableBertConfig(SimpleNamespace):
    def __init__(
        self,
        base_model_name: str = 'bert-base-uncased',
        column_delimiter: str = '[SEP]',
        context_first: bool = True,
        cell_input_template: bool = 'column|value|cell',
        column_representation: str = 'mean_pool',
        max_cell_value_token_num: int = 5
    ):
        super(TableBertConfig, self).__init__()

        self.base_model_name = base_model_name
        self.column_delimiter = column_delimiter
        self.context_first = context_first
        self.column_representation = column_representation
        self.max_cell_value_token_num = max_cell_value_token_num

        tokenizer = BertTokenizer.from_pretrained(self.base_model_name)
        self.cell_input_template = tokenizer.tokenize(cell_input_template)

    @classmethod
    def from_file(cls, file_path: Union[str, Path], override_args: Dict = None):
        if isinstance(file_path, str):
            file_path = Path(file_path)

        args = json.load(file_path.open())
        override_args = override_args or dict()
        args.update(override_args)
        default_config = TableBertConfig()
        config_dict = {}
        for key, default_val in vars(default_config).items():
            val = args.get(key, default_val)
            config_dict[key] = val

        # backward compatibility
        if 'column_item_delimiter' in args:
            column_item_delimiter = args['column_item_delimiter']
            cell_input_template = 'column'
            use_value = args.get('use_sample_value', True)
            use_type = args.get('use_type_text', True)

            if use_value:
                cell_input_template += column_item_delimiter + 'value'
            if use_type:
                cell_input_template += column_item_delimiter + 'type'

            config_dict['cell_input_template'] = cell_input_template

        config = cls(**config_dict)

        return config


class TableBERT(nn.Module):
    def __init__(
        self,
        bert_model: BertForPreTraining,
        config: TableBertConfig,
        **kwargs
    ):
        super(TableBERT, self).__init__()
        self._bert_model: BertForPreTraining = bert_model
        self.tokenizer = BertTokenizer.from_pretrained(config.base_model_name)
        self.config = config

    @property
    def bert(self):
        return self._bert_model.bert

    @property
    def bert_config(self):
        return self.bert.config

    @property
    def output_size(self):
        return self.bert.config.hidden_size

    @property
    def device(self):
        return next(self.parameters()).device

    @classmethod
    def load(
        cls,
        model_path: Union[str, Path],
        config_file: Union[str, Path],
        **override_config: Dict
    ):
        if model_path and isinstance(model_path, str):
            model_path = Path(model_path)
        if isinstance(config_file, str):
            config_file = Path(config_file)

        if model_path:
            state_dict = torch.load(str(model_path), map_location='cpu')
        else:
            state_dict = None

        config = TableBertConfig.from_file(config_file, override_config)
        bert_model = BertForPreTraining.from_pretrained(
            config.base_model_name,
            state_dict=state_dict
        )

        model = cls(bert_model, config)

        return model

    def encode_context_and_table(
        self,
        input_ids: torch.Tensor,
        segment_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        context_token_indices: torch.Tensor,
        context_token_mask: torch.Tensor,
        column_token_mask: torch.Tensor,
        column_token_to_column_id: torch.Tensor,
        column_mask: torch.Tensor,
        **kwargs
    ):

        # print('input_ids', input_ids.size(), file=sys.stderr)
        # print('segment_ids', segment_ids.size(), file=sys.stderr)
        # print('attention_mask', attention_mask.size(), file=sys.stderr)
        # print('column_token_mask', column_token_mask.size(), file=sys.stderr)
        # print('column_token_mask', column_token_mask.sum(dim=-1), file=sys.stderr)
        # print('column_token_to_column_id', column_token_to_column_id.size(), file=sys.stderr)
        # print('column_token_to_column_id', column_token_to_column_id.sum(dim=-1), file=sys.stderr)
        # print('column_mask', column_mask.size(), file=sys.stderr)

        # try:
        sequence_output, _ = self.bert(input_ids, segment_ids, attention_mask, output_all_encoded_layers=False)
        # except:
        #     print('!!!!!Exception!!!!!')
        #     datum = (input_ids, segment_ids, attention_mask, question_token_mask,
        #              column_token_mask, column_token_to_column_id, column_mask)
        #     torch.save(datum, 'debug.tensors.bin')
        #     raise

        # gather column representations
        # (batch_size, max_seq_len, encoding_size)
        flattened_column_encoding = sequence_output
        # (batch_size, max_column_size, encoding_size)
        column_encoding = get_column_representation(flattened_column_encoding,
                                                    column_token_to_column_id,
                                                    column_token_mask,
                                                    column_mask,
                                                    aggregator=self.config.column_representation)

        # (batch_size, context_len, encoding_size)
        context_encoding = torch.gather(
            sequence_output,
            dim=1,
            index=context_token_indices.unsqueeze(-1).expand(-1, -1, sequence_output.size(-1)),
        )
        context_encoding = context_encoding * context_token_mask.unsqueeze(-1)

        return context_encoding, column_encoding

    def get_cell_bert_input_tokens(
        self,
        column: Column,
        cell_value: List[str],
        token_offset: int
    ):
        input = []
        span_map = {
            'first_token': (token_offset, token_offset + 1)
        }

        for token in self.config.cell_input_template:
            start_token_abs_position = len(input) + token_offset
            if token == 'column':
                span_map['column_name'] = (start_token_abs_position,
                                           start_token_abs_position + len(column.name_tokens))
                input.extend(column.name_tokens)
            elif token == 'value':
                span_map['value'] = (start_token_abs_position,
                                     start_token_abs_position + len(cell_value))
                input.extend(cell_value)
            elif token == 'type':
                span_map['type'] = (start_token_abs_position,
                                    start_token_abs_position + len(cell_value))
                input.append(column.type)
            else:
                input.append(token)

        span_map['whole_span'] = (token_offset, token_offset + len(input))

        return input, span_map

    def convert_row_to_bert_input(
        self,
        context_tokens: List[str],
        row_data: Dict,
        header: List[Column],
    ) -> Dict:

        if self.config.context_first:
            table_tokens_start_idx = len(context_tokens) + 2  # account for [CLS] and [SEP]
            # account for [CLS] and [SEP], and the ending [SEP]
            max_table_token_length = MAX_BERT_INPUT_LENGTH - len(context_tokens) - 2 - 1
        else:
            table_tokens_start_idx = 1  # account for starting [CLS]
            # account for [CLS] and [SEP], and the ending [SEP]
            max_table_token_length = MAX_BERT_INPUT_LENGTH - len(context_tokens) - 2 - 1

        # generate table tokens
        row_input_tokens = []
        column_token_span_maps = {}
        column_start_idx = table_tokens_start_idx

        for col_id, column in enumerate(header):
            value_tokens = row_data[column.name]
            truncated_value_tokens = value_tokens[:self.config.max_cell_value_token_num]

            column_input_tokens, token_span_map = self.get_cell_bert_input_tokens(
                column,
                truncated_value_tokens,
                token_offset=column_start_idx
            )

            if len(row_input_tokens) + len(column_input_tokens) > max_table_token_length:
                break

            row_input_tokens.extend(column_input_tokens)
            column_start_idx = column_start_idx + len(column_input_tokens)
            column_token_span_maps[column.name] = token_span_map

        if row_input_tokens[-1] == self.config.column_delimiter:
            del row_input_tokens[-1]

        if self.config.context_first:
            sequence = ['[CLS]'] + context_tokens + ['[SEP]'] + row_input_tokens + ['[SEP]']
            segment_ids = [0] * (len(context_tokens) + 2) + [1] * (len(row_input_tokens) + 1)
            context_token_indices = list(range(0, 1 + len(context_tokens)))
        else:
            sequence = ['[CLS]'] + row_input_tokens + ['[SEP]'] + context_tokens + ['[SEP]']
            segment_ids = [0] * (len(row_input_tokens) + 2) + [1] * (len(context_tokens) + 1)
            context_token_indices = list(range(len(row_input_tokens) + 2, len(row_input_tokens) + 2 + 1 + len(context_tokens)))

        instance = {
            'tokens': sequence,
            'segment_ids': segment_ids,
            'column_spans': column_token_span_maps,
            'context_length': 1 + len(context_tokens),  # [CLS]/[SEP] + input question
            'context_token_indices': context_token_indices
        }

        assert len(context_token_indices) == instance['context_length']

        return instance

    def convert_example_to_bert_input(
        self,
        example: Example
    ) -> Dict:
        pseudo_row = {column.name: column.sample_value_tokens for column in example.table.header}
        instance = self.convert_row_to_bert_input(example.question, pseudo_row, example.table.header)

        return instance

    # noinspection PyUnboundLocalVariable
    def to_tensor_dict(self,
        examples: List[Example], table_specific_tensors=True
    ):
        instances = []
        for e_id, example in enumerate(examples):
            instance = self.convert_example_to_bert_input(example)
            instances.append(instance)

        batch_size = len(examples)
        max_sequence_len = max(len(x['tokens']) for x in instances)

        # basic tensors
        input_array = np.zeros((batch_size, max_sequence_len), dtype=np.int)
        mask_array = np.zeros((batch_size, max_sequence_len), dtype=np.bool)
        segment_array = np.zeros((batch_size, max_sequence_len), dtype=np.bool)

        # table specific tensors
        if table_specific_tensors:
            max_column_num = max(len(x['column_spans']) for x in instances)
            max_context_len = max(x['context_length'] for x in instances)

            context_token_indices = np.zeros((batch_size, max_context_len), dtype=np.int)
            context_mask = np.zeros((batch_size, max_context_len), dtype=np.bool)
            column_token_mask = np.zeros((batch_size, max_sequence_len), dtype=np.bool)

            # we initialize the mapping with the id of last column as the "garbage collection" entry for reduce ops
            column_token_to_column_id = np.zeros((batch_size, max_sequence_len), dtype=np.int)
            column_token_to_column_id.fill(max_column_num)

            column_mask = np.zeros((batch_size, max_column_num), dtype=np.bool)

            column_span = 'whole_span'
            if 'column_name' in self.config.column_representation:
                column_span = 'column_name'
            elif 'first_token' in self.config.column_representation:
                column_span = 'first_token'

        for i, instance in enumerate(instances):
            token_ids = self.tokenizer.convert_tokens_to_ids(instance['tokens'])
            segment_ids = instance['segment_ids']

            input_array[i, :len(token_ids)] = token_ids
            segment_array[i, :len(segment_ids)] = segment_ids
            mask_array[i, :len(token_ids)] = 1.

            if table_specific_tensors:
                context_token_indices[i, :instance['context_length']] = instance['context_token_indices']
                context_mask[i, :instance['context_length']] = 1.

                header = examples[i].table.header
                for col_id, column in enumerate(header):
                    if column.name in instance['column_spans']:
                        col_start, col_end = instance['column_spans'][column.name][column_span]

                        column_token_to_column_id[i, col_start: col_end] = col_id
                        column_token_mask[i, col_start: col_end] = 1.
                        column_mask[i, col_id] = 1.

        tensor_dict = {
            'input_ids': torch.tensor(input_array.astype(np.int64)),
            'segment_ids': torch.tensor(segment_array.astype(np.int64)),
            'attention_mask': torch.tensor(mask_array, dtype=torch.float32),
        }

        if table_specific_tensors:
            tensor_dict.update({
                'context_token_indices': torch.tensor(context_token_indices.astype(np.int64)),
                'context_token_mask': torch.tensor(context_mask, dtype=torch.float32),
                'column_token_to_column_id': torch.tensor(column_token_to_column_id.astype(np.int64)),
                'column_token_mask': torch.tensor(column_token_mask, dtype=torch.float32),
                'column_mask': torch.tensor(column_mask, dtype=torch.float32)
            })

        # for instance in instances:
        #     print(instance)

        return tensor_dict, instances

    def encode(self, examples: List[Example]):
        tensor_dict, instances = self.to_tensor_dict(examples)
        device = next(self.parameters()).device

        for key in tensor_dict.keys():
            tensor_dict[key] = tensor_dict[key].to(device)

        context_encoding, column_encoding = self.encode_context_and_table(
            **tensor_dict)

        info = {
            'tensor_dict': tensor_dict,
            'instances': instances
        }

        return context_encoding, column_encoding, info


class ContentEncodingTableBERT(TableBERT):
    def to_tensor_dict(self, examples: List[Example]):
        row_examples = []
        row_to_table_map = []
        example_first_row_id = []
        max_row_num = max(len(e.table) for e in examples)

        for e_id, example in enumerate(examples):
            table = example.table
            example_first_row_id.append(len(row_examples))

            for row_id, row in enumerate(table.data):
                new_header = []
                for column in table.header:
                    new_col = Column(
                        column.name, column.type, row[column.name],
                        name_tokens=column.name_tokens, sample_value_tokens=row[column.name]
                    )
                    new_header.append(new_col)

                row_tb = Table(row_id, new_header)
                row_example = Example(example.question, row_tb)

                row_examples.append(row_example)
                row_to_table_map.append(e_id)

        tensor_dict, row_instances = TableBERT.to_tensor_dict(self, row_examples)

        column_mask = tensor_dict['column_mask']
        table_mask = np.zeros((len(examples), max_row_num, column_mask.size(-1)), dtype=np.float32)
        for e_id, example in enumerate(examples):
            first_row_flattened_pos = example_first_row_id[e_id]
            table_mask[e_id, :len(example.table)] = column_mask[first_row_flattened_pos]

        tensor_dict['table_mask'] = torch.from_numpy(table_mask).to(self.device)

        # (total_row_num)
        tensor_dict['row_to_table_map'] = torch.tensor(row_to_table_map, dtype=torch.long, device=self.device)
        tensor_dict['example_first_row_id'] = torch.tensor(example_first_row_id, dtype=torch.long, device=self.device)

        return tensor_dict

    def flattened_row_encoding_to_table_encoding(self, row_encoding, table_sizes):
        table_num = len(table_sizes)
        max_row_num = max(table_sizes)

        scatter_indices = np.zeros(row_encoding.size(0), dtype=np.int64)
        cum_size = 0
        for table_id, tb_size in enumerate(table_sizes):
            scatter_indices[cum_size: cum_size + tb_size] = list(
                range(table_id * max_row_num, table_id * max_row_num + tb_size))
            cum_size += tb_size

        scatter_indices = torch.from_numpy(scatter_indices).to(self.device)

        out = row_encoding.new_zeros(
            table_num * max_row_num,
            row_encoding.size(-2),  # max_column_num
            row_encoding.size(-1)   # encoding_size
        )
        out[scatter_indices] = row_encoding

        table_encoding = out.view(table_num, max_row_num, row_encoding.size(-2), row_encoding.size(-1))

        return table_encoding

    def encode(self, examples: List[Example]):
        row_tensor_dict = self.to_tensor_dict(examples)

        for key in row_tensor_dict.keys():
            row_tensor_dict[key] = row_tensor_dict[key].to(self.device)

        # (total_row_num, sequence_len, ...)
        row_context_encoding, row_encoding = self.encode_context_and_table(
            **row_tensor_dict)

        # (batch_size, context_len, ...)
        context_encoding, max_row_id = scatter_max(row_context_encoding, index=row_tensor_dict['row_to_table_map'], dim=0)

        example_first_row_indices = row_tensor_dict['example_first_row_id']
        # (batch_size, context_len)
        context_mask = row_tensor_dict['context_token_mask'][example_first_row_indices]

        context_encoding = {
            'value': context_encoding,
            'mask': context_mask,
        }

        # (batch_size, row_num, column_num, encoding_size)
        table_encoding_var = self.flattened_row_encoding_to_table_encoding(
            row_encoding, [len(e.table) for e in examples])
        # max_row_num = table_encoding_var.size(1)
        # table_column_mask = row_tensor_dict['column_mask'][example_first_row_indices]

        table_encoding = {
            'value': table_encoding_var,
            'mask': row_tensor_dict['table_mask'],
            'column_mask': row_tensor_dict['column_mask']
            #  'row_encoding': row_encoding,
            #  'row_encoding_mask': row_tensor_dict['column_mask']
        }

        info = {
            'tensor_dict': row_tensor_dict,
        }

        return context_encoding, table_encoding, info


class BERTRelationIdentificationModel(BertPreTrainedModel):
    def __init__(self, config, output_dropout_prob=0.1, column_representation='max_pool', **kwargs):
        super(BERTRelationIdentificationModel, self).__init__(config)
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(output_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, 2)
        self.apply(self.init_bert_weights)

        self.column_representation = column_representation

    @classmethod
    def config(cls):
        return {
            'output_dropout_prob': 0.1,
            'column_representation': 'max_pooling'
        }

    @classmethod
    def build(cls, model_path):
        if isinstance(model_path, str):
            model_path = Path(model_path)

        output_config_file = model_path.parent / CONFIG_NAME
        print(f'BERT config file: {output_config_file}', file=sys.stderr)
        bert_config = BertConfig(str(output_config_file))

        config_file = model_path.parent / 'config.json'
        print(f'Model config file: {config_file}', file=sys.stderr)
        config = json.load(config_file.open())
        model = cls(bert_config, **config)

        print(f'model file: {model_path}', file=sys.stderr)
        model.load_state_dict(torch.load(str(model_path), map_location=lambda storage, location: storage))

        return model

    def get_column_representation(self,
                                  flattened_column_encoding: torch.Tensor,
                                  column_token_to_column_id: torch.Tensor,
                                  column_token_mask: torch.Tensor,
                                  column_mask: torch.Tensor) -> torch.Tensor:
        """
        Args:
            flattened_column_encoding: (batch_size, total_column_token_num, encoding_size)
            column_token_to_column_id: (batch_size, total_column_token_num)
            column_mask: (batch_size, max_column_num)

        Returns:
            column_encoding: (batch_size, max_column_num, encoding_size)
        """

        method = self.column_representation
        if method == 'max_pool':
            agg_func = scatter_max
            flattened_column_encoding[column_token_mask == 0] = float('-inf')
        elif method == 'mean_pool':
            agg_func = scatter_mean

        max_column_num = column_mask.size(-1)
        # column_token_to_column_id: (batch_size, max_column_num)
        # (batch_size, max_column_size, encoding_size)
        result = agg_func(flattened_column_encoding,
                          column_token_to_column_id.unsqueeze(-1).expand(-1, -1, self.config.hidden_size),
                          dim=1,
                          dim_size=max_column_num)

        if method == 'max_pool':
            column_encoding = result[0]
        else:
            column_encoding = result

        return column_encoding

    def forward(self, input_ids, token_type_ids, attention_mask, column_token_to_column_id, column_token_mask, column_mask, labels=None, **kwargs):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)

        # grab column representations
        # (batch_size, max_seq_len, encoding_size)
        flattened_column_encoding = sequence_output
        # (batch_size, max_column_size, encoding_size)
        column_encoding = self.get_column_representation(flattened_column_encoding,
                                                         column_token_to_column_id,
                                                         column_token_mask,
                                                         column_mask)

        logits = self.classifier(column_encoding)
        info = dict()

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if column_mask is not None:
                active_loss = column_mask.view(-1) == 1
                active_logits = logits.view(-1, 2)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, 2), labels.view(-1))
            return loss, info
        else:
            return logits, info


class BERTRelationIdentificationAlignmentBasedModel(BertPreTrainedModel):
    def __init__(self, config, attention_type='biaffine'):
        super(BERTRelationIdentificationAlignmentBasedModel, self).__init__(config)
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        self.attention_type = attention_type
        if attention_type == 'biaffine':
            self.att_linear = nn.Linear(config.hidden_size, config.hidden_size)

        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids, attention_mask,
                column_token_to_column_id, column_token_mask, column_mask, question_token_mask,
                labels=None, return_attention_matrix=False):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)

        # (batch_size, max_question_len, encoding_size)
        max_question_len = question_token_mask.size(-1)
        question_token_encoding = sequence_output[:, :max_question_len].clone()

        # grab column representations
        # (batch_size, max_seq_len, encoding_size)
        flattened_column_encoding = sequence_output
        flattened_column_encoding[column_token_mask == 0] = float('-inf')
        # column_token_to_column_id: (batch_size, max_column_num)
        # (batch_size, max_column_size, encoding_size)
        column_encoding, _ = scatter_max(flattened_column_encoding,
                                         column_token_to_column_id.unsqueeze(-1).expand(-1, -1, self.config.hidden_size),
                                         dim=1, dim_size=column_mask.size(-1))

        # (batch_size, max_question_len, max_column_num)
        att_weights_matrix = self.attention(question_token_encoding, column_encoding.permute(0, 2, 1))
        att_weights = (1. - question_token_mask.unsqueeze(-1)) * NEGATIVE_NUMBER + att_weights_matrix
        # (batch_size, max_column_num)
        att_weights, _ = torch.max(att_weights, dim=1)

        pred_info = dict()
        if labels is not None:
            loss_fct = BCEWithLogitsLoss()
            # Only keep active parts of the loss
            if column_mask is not None:
                active_loss = column_mask.view(-1) == 1
                active_logits = att_weights.view(-1)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels.float())
            else:
                loss = loss_fct(att_weights.view(-1), labels.view(-1))
            return loss, pred_info
        else:
            p = torch.sigmoid(att_weights)
            result = torch.stack([1 - p, p], dim=-1)
            if return_attention_matrix:
                pred_info['attention_matrix'] = att_weights_matrix

            return result, pred_info

    def attention(self, key, value):
        if self.attention_type == 'biaffine':
            key = self.att_linear(key)

        return torch.bmm(key, value)
