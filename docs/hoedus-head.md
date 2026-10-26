# Hoedus as the Ray head

The repository defaults to Diya (`cisc-diya`, `10.10.10.1`) as the Ray head and
Hoedus (`cisc-hoedus`, `10.10.10.2`) as the worker. The
`hoedus-head-env.sh` environment file assigns Hoedus the head role and Diya the
worker role. `hoedus-head` sources the same file.

Run the commands from the repository root. Source `./hoedus-head-env.sh` in each
shell that runs a repository command. The file exports `HEAD_IP=10.10.10.2` and
`HOEDUS_IP=10.10.10.1`. `HEAD_IP` must be the local ConnectX address on the
head. `HOEDUS_IP` must be the worker address.

## Start Ray

Use the same repository revision and environment mapping on both nodes. On
Hoedus, run:

```bash
source ./hoedus-head-env.sh
scripts/start_ray.sh head
```

On Diya, run:

```bash
source ./hoedus-head-env.sh
scripts/start_ray.sh worker
```

Check the cluster from Hoedus:

```bash
source ./hoedus-head-env.sh
docker exec vllm-spark ray status
```

Ray must report two active nodes before starting a distributed serving run.

## Run a serving test

Run a targeted `tp2` server canary from Hoedus. The canary launches the selected
model and validates serving without running the measurement sweep:

```bash
source ./hoedus-head-env.sh
OUT_DIR=results/hoedus-head-test MODEL_LABELS=llama3.1-8b-fp8 CANARY_ONLY=1 \
  scripts/run_models.sh tp2
```

The command acquires the pinned model snapshot on Hoedus, mirrors it to Diya,
launches the two-node server, and removes the model after the canary. The test
path must be unused.

## Run a campaign

Run the campaign plan from Hoedus before starting the full campaign:

```bash
source ./hoedus-head-env.sh
OUT_DIR=results/hoedus-head-campaign PLAN_ONLY=1 scripts/run_job.sh
```

`PLAN_ONLY=1` checks the campaign inputs without starting hardware work. Run the
full campaign from Hoedus inside `tmux` with the same environment mapping and
output path:

```bash
source ./hoedus-head-env.sh
OUT_DIR=results/hoedus-head-campaign scripts/run_job.sh
```

The campaign runs on Hoedus as the benchmark client and Ray head. Ray uses Diya
as the worker for `tp2` and `pp2` serving. Set `OUT_DIR` to an unused path for a
new campaign. Use the same path and environment mapping to resume it.

`run_job.sh` requires a clean benchmark source tree under `bench`, `configs`,
`scripts`, `plotting`, and the requirements files. The root-level `hoedus-head`
and `hoedus-head-env.sh` files and this documentation page are outside that
check.
