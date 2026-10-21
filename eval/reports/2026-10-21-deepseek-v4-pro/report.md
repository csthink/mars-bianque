# Bianque evaluation

- Result: passed
- Model mode: live; model: deepseek-v4-pro
- Embeddings: fixture (BAAI/bge-small-en-v1.5)
- Generated at 2026-10-21T02:11:12+00:00

## Hard gates

| gate | result | details |
| --- | --- | --- |
| investigations_report | pass | - |
| outputs_pass_the_gate | pass | - |
| no_write_tools | pass | - |
| no_agent_evidence | pass | - |
| knowledge_not_cited | pass | - |

## Summary

| metric | value |
| --- | --- |
| root cause correct | 100.00% (6/6) |
| citation completeness (first report) | 100.00% (112/112) |
| required evidence coverage | 100.00% (17/17) |
| gate first pass: plan | 100.00% (14/14) |
| gate first pass: findings | 92.31% (48/52) |
| gate first pass: assessment | 100.00% (14/14) |
| gate first pass: report | 100.00% (6/6) |
| gate first pass: followup_answer | 94.74% (18/19) |
| follow-up cited segments | 100.00% (63/63) |
| follow-up expected evidence cited | 100.00% (10/10) |
| follow-up unknown answered as unknown | 100.00% (7/7) |
| tokens | 630926 |
| model calls | 201 |

## Cases

| case | status | root cause | correct | citations | required evidence | report attempts | tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| s1-a | reported | config_error | yes | 100.00% (19/19) | 100.00% (4/4) | 1 | 82922 / 120000 |
| s1-b | reported | resource_limit | yes | 100.00% (18/18) | 100.00% (3/3) | 1 | 77132 / 120000 |
| s1-c | reported | probe_misconfig | yes | 100.00% (19/19) | 100.00% (3/3) | 1 | 99177 / 120000 |
| s1-d | reported | config_error | yes | 100.00% (22/22) | 100.00% (3/3) | 1 | 147420 / 120000 |
| s1-e | reported | config_error | yes | 100.00% (17/17) | 100.00% (4/4) | 1 | 86835 / 120000 |
| s1-f | reported | unknown | yes | 100.00% (17/17) | - (0/0) | 1 | 137440 / 120000 |

## Follow-up questions

| case | question | outcome | cited segments | expected evidence | suggestion | unknown | expected unknown |
| --- | --- | --- | --- | --- | --- | --- | --- |
| s1-a | q1 | answered | 100.00% (5/5) | yes | - | no | no |
| s1-a | q2 (follows q1) | answered | 100.00% (4/4) | yes | - | no | no |
| s1-a | q3 | answered | 100.00% (5/5) | - | yes | no | no |
| s1-a | q4 | answered | 100.00% (2/2) | - | - | yes | yes |
| s1-b | q1 | answered | 100.00% (5/5) | yes | - | no | no |
| s1-b | q2 | answered | 100.00% (3/3) | yes | - | no | no |
| s1-b | q3 | answered | - (0/0) | - | - | yes | yes |
| s1-c | q1 | answered | 100.00% (5/5) | yes | - | yes | no |
| s1-c | q2 (follows q1) | answered | 100.00% (4/4) | yes | - | no | no |
| s1-c | q3 | answered | 100.00% (1/1) | - | - | yes | yes |
| s1-d | q1 | answered | 100.00% (5/5) | yes | - | no | no |
| s1-d | q2 | answered | 100.00% (2/2) | - | - | yes | yes |
| s1-d | q3 | answered | 100.00% (4/4) | - | no | yes | no |
| s1-e | q1 | answered | 100.00% (3/3) | yes | - | no | no |
| s1-e | q2 (follows q1) | answered | 100.00% (3/3) | yes | - | no | no |
| s1-e | q3 | answered | 100.00% (1/1) | - | - | yes | yes |
| s1-f | q1 | answered | 100.00% (7/7) | yes | - | yes | no |
| s1-f | q2 | answered | 100.00% (2/2) | - | - | yes | yes |
| s1-f | q3 | answered | 100.00% (2/2) | - | - | yes | yes |

## Retrieval

| metric | value |
| --- | --- |
| hit_at_1 | 0.7917 |
| hit_at_3 | 0.8333 |
| hit_at_5 | 0.9167 |
| mrr | 0.8264 |
