#!/bin/sh
# properties = {"type": "single", "rule": "train_prior", "local": false, "input": [], "output": ["outputs/esm2_64_sigmoid.out"], "wildcards": {"lm": "esm2", "dimension": "64", "activation": "sigmoid"}, "params": {}, "log": [], "threads": 4, "resources": {"mem_mb": 100000, "mem_mib": 95368, "disk_mb": 1000, "disk_mib": 954, "tmpdir": "<TBD>", "partition": "vision", "gpu": 1, "runtime": 4320}, "jobid": 6, "cluster": {}}
cd '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models' && /home/beckerf/mambaforge/envs/snakeMSA/bin/python3.10 -m snakemake --snakefile '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/Snakefile' --target-jobs 'train_prior:lm=esm2,dimension=64,activation=sigmoid' --allowed-rules 'train_prior' --cores 'all' --attempt 3 --force-use-threads  --resources 'mem_mb=100000' 'mem_mib=95368' 'disk_mb=1000' 'disk_mib=954' 'gpu=1' --wait-for-files '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/.snakemake/tmp.ibjtv61j' --force --keep-target-files --keep-remote --max-inventory-time 0 --nocolor --notemp --no-hooks --nolock --ignore-incomplete --rerun-triggers 'code' 'input' 'software-env' 'mtime' 'params' --skip-script-cleanup  --use-conda  --conda-frontend 'mamba' --conda-base-path '/home/beckerf/mambaforge/envs/snakeMSA' --wrapper-prefix 'https://github.com/snakemake/snakemake-wrappers/raw/' --configfiles '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/config.yml' --printshellcmds  --latency-wait 60 --scheduler 'greedy' --scheduler-solver-path '/home/beckerf/mambaforge/envs/snakeMSA/bin' --default-resources 'mem_mb=10000' 'disk_mb=max(2*input.size_mb, 1000)' 'tmpdir=system_tmpdir' 'partition=pinky' 'gpu=0' --mode 2 && touch '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/.snakemake/tmp.ibjtv61j/6.jobfinished' || (touch '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/.snakemake/tmp.ibjtv61j/6.jobfailed'; exit 1)

