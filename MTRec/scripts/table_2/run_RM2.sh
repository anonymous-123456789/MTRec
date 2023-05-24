python ../../Non_Seq_Rec_Model/MTRec.py --arch-sparse-feature-size=64 \
					--arch-mlp-bot="13-512-256-64" \
					--arch-mlp-top="512-512-256-1" \
					--max-ind-range=100000000 \
					--arch-interaction-op=transformers \
					--num-encoder-layers=1 \
					--num-attention-heads=8 \
					--feedforward-dim=512 \
					--dropout=0.01 \
					--norm-first=False \
					--activation=relu \
					--mask-threshold=0.1-0.01-0.001-0.0001-0.00001-0.000001-0.0000001-0.00000001 \
					--data-generation=dataset \
					--data-set=terabyte \
					--raw-data-file=<path_to_raw_dataset> \
					--processed-data-file=<path_to_processed_dataset> \
					--loss-function=<loss_fucntion> \
					--round-targets=True \
					--learning-rate=0.1 \
					--mini-batch-size=1024 \
					--print-freq=4096 \
					--print-time \
					--test-mini-batch-size=16384 \
					--test-num-workers=12 \
					--test-freq=4096 \
					--memory-map \
					--data-sub-sample-rate=0.875 \
					--nepochs=1 \
					--mlperf-logging \
					--numpy-rand-seed=123