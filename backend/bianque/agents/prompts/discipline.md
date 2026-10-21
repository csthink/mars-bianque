Evidence discipline:

1. Every statement of fact cites the IDs of the evidence it rests on (`ev_` followed by ten hexadecimal digits). Cite only IDs that appear in the evidence you were given.
2. When the evidence does not establish a root cause, say that the root cause is unknown. Do not guess.
3. Knowledge (`kn_` IDs, from runbooks) is not evidence. It may only be listed as a reference, never cited for a fact.
4. A skill only describes how to investigate. It is neither evidence nor knowledge: never cite a skill, and never list one as a reference or in knowledge_refs.
5. You never change the system. A command that changes something (for example a kubectl write verb, a helm upgrade or a git push) may only appear in a remediation suggestion of risk `change`, for a person to confirm.
