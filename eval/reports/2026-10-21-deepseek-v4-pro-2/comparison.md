# Evaluation comparison

- Baseline: deepseek-v4-pro in live mode, generated at 2026-10-21T02:11:12+00:00 (report format 1)
- New: deepseek-v4-pro in live mode, generated at 2026-10-21T14:43:33+00:00 (report format 2)

> Both reports come from one live run each; they do not show how the model behaves on every run. Tokens are those of the investigation; model calls include the follow-up questions.

| case | root cause (baseline) | root cause (new) | citations (baseline) | citations (new) | required evidence (baseline) | required evidence (new) | tokens (baseline) | tokens (new) | model calls (baseline) | model calls (new) | skills (baseline) | skills (new) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| s1-a | config_error, correct | config_error, correct | 100.00% (19/19) | 100.00% (19/19) | 100.00% (4/4) | 100.00% (4/4) | 82922 | 85923 | 27 | 29 | not recorded | attribute-gitops-change, investigate-pod-restarts |
| s1-b | resource_limit, correct | resource_limit, correct | 100.00% (18/18) | 100.00% (16/16) | 100.00% (3/3) | 100.00% (3/3) | 77132 | 99790 | 25 | 34 | not recorded | attribute-gitops-change, investigate-pod-restarts |
| s1-c | probe_misconfig, correct | probe_misconfig, correct | 100.00% (19/19) | 100.00% (17/17) | 100.00% (3/3) | 100.00% (3/3) | 99177 | 92736 | 29 | 29 | not recorded | attribute-gitops-change, investigate-pod-restarts |
| s1-d | config_error, correct | config_error, correct | 100.00% (22/22) | 100.00% (19/19) | 100.00% (3/3) | 100.00% (3/3) | 147420 | 110419 | 43 | 34 | not recorded | attribute-gitops-change, diagnose-config-error, investigate-pod-restarts |
| s1-e | config_error, correct | config_error, correct | 100.00% (17/17) | 100.00% (18/18) | 100.00% (4/4) | 100.00% (4/4) | 86835 | 88845 | 27 | 29 | not recorded | attribute-gitops-change, investigate-pod-restarts |
| s1-f | unknown, correct | unknown, correct | 100.00% (17/17) | 100.00% (17/17) | - (0/0) | - (0/0) | 137440 | 140410 | 50 | 50 | not recorded | attribute-gitops-change, diagnose-config-error, investigate-pod-restarts |
| all | 100.00% (6/6) | 100.00% (6/6) | 100.00% (112/112) | 100.00% (106/106) | 100.00% (17/17) | 100.00% (17/17) | 630926 | 618123 | 201 | 205 | not recorded | 14 |
