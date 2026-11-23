# Notes

## Running 90% mem usage with small model subset, off disk
```bash
MODELS_ONDISK=0 \
MODELS_YAML=configs/models_short.yaml \
GPU_UTIL=0.90 \
CONCURRENCIES="1 4 8 16 32 64 128" \
REPEATS=2 \
CAMPAIGN_CONFIGS="tp2 pp2" \
scripts/run_preflight_then_benchmark.sh
```
