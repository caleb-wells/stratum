| Setup | Correct | Wrong-regulation errors | Retrieved the right section | Avg input tokens |
|---|---|---|---|---|
| No retrieval | 21% (4/19) | 37% (7/19) | n/a | 575 |
| Naive RAG (one pooled search) | 68% (13/19) | 26% (5/19) | 69% (11/16) | 9,224 |
| Stratum (one search per regulation) | 95% (18/19) | 0% (0/19) | 94% (15/16) | 14,203 |

Correct answers by question type:

| Setup | Single | Compare | Silent | Draft |
|---|---|---|---|---|
| No retrieval | 20% (2/10) | 0% (0/4) | 50% (2/4) | 0% (0/1) |
| Naive RAG (one pooled search) | 100% (10/10) | 25% (1/4) | 50% (2/4) | 0% (0/1) |
| Stratum (one search per regulation) | 100% (10/10) | 100% (4/4) | 100% (4/4) | 0% (0/1) |

Misses by Stratum:

- `draft-requirements-summary`: The answer wrongly says the Draper City documents specify no fault setback, when the gold answer gives Lund and others (2020) compared against the Fault Setback Table with a 15 ft minimum, and it also omits the Batatian and Nelson (1999) / Christenson and others (2003) methods and Table A-1 for Fruit Heights City, though no values are attributed to the wrong jurisdiction.
