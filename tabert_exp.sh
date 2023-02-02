OMP_NUM_THREADS=1 python\
  experiments.py \
  train \
  --seed 0 \
  --cuda \
  --work-dir=runs/demo_run \
  --config=data/config/config.table_bert.json

