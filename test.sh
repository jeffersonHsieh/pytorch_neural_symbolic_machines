WORK_DIR=runs/demo_run
python experiments.py \
  test \
  --cuda \
  --model $WORK_DIR/model.best.bin \
  --test-file data/wikitable/wtq_preprocess_0805_no_anonymize_ent/test_split.jsonl \
  --save-decode-to $WORK_DIR/test_decode_iter20000.json

