#!/bin/sh
# properties = {"type": "single", "rule": "train_scoring_model", "local": false, "input": [], "output": ["outputs/proteinBERT_128_softmax.out"], "wildcards": {"lm": "proteinBERT", "dimension": "128", "activation": "softmax"}, "params": {}, "log": [], "threads": 4, "resources": {"mem_mb": 100000, "mem_mib": 95368, "disk_mb": 1000, "disk_mib": 954, "tmpdir": "<TBD>", "partition": "vision", "gpu": 1, "runtime": 4320}, "jobid": 23, "cluster": {}}
cd '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models' && /home/beckerf/mambaforge/envs/snakeMSA/bin/python3.10 -m snakemake --snakefile '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/Snakefile' --target-jobs 'train_scoring_model:lm=proteinBERT,dimension=128,activation=softmax' --allowed-rules 'train_scoring_model' --cores 'all' --attempt 1 --force-use-threads  --resources 'mem_mb=100000' 'mem_mib=95368' 'disk_mb=1000' 'disk_mib=954' 'gpu=1' --wait-for-files '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/.snakemake/tmp.z5is458p' --force --keep-target-files --keep-remote --max-inventory-time 0 --nocolor --notemp --no-hooks --nolock --ignore-incomplete --rerun-triggers 'input' 'mtime' 'params' 'code' 'software-env' --skip-script-cleanup  --use-conda  --conda-frontend 'mamba' --conda-base-path '/home/beckerf/mambaforge/envs/snakeMSA' --wrapper-prefix 'https://github.com/snakemake/snakemake-wrappers/raw/' --configfiles '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/config.yml' --printshellcmds  --latency-wait 60 --scheduler 'greedy' --scheduler-solver-path '/home/beckerf/mambaforge/envs/snakeMSA/bin' --default-resources 'mem_mb=10000' 'disk_mb=max(2*input.size_mb, 1000)' 'tmpdir=system_tmpdir' 'partition=pinky' 'gpu=0' --mode 2 && touch '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/.snakemake/tmp.z5is458p/23.jobfinished' || (touch '/home/beckerf/tmp_work/learnMSA/learnMSA/protein_language_models/train_scoring_models/.snakemake/tmp.z5is458p/23.jobfailed'; exit 1)

