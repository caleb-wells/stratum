| Setup | Correct | Wrong-regulation errors | Retrieved the right section | Avg input tokens |
|---|---|---|---|---|
| No retrieval | 16% (3/19) | 42% (8/19) | n/a | 575 |
| Naive RAG (one pooled search) | 68% (13/19) | 11% (2/19) | 69% (11/16) | 9,224 |
| Stratum (one search per regulation) | 100% (19/19) | 11% (2/19) | 94% (15/16) | 15,589 |

Correct answers by question type:

| Setup | Single | Compare | Silent | Draft |
|---|---|---|---|---|
| No retrieval | 20% (2/10) | 0% (0/4) | 25% (1/4) | 0% (0/1) |
| Naive RAG (one pooled search) | 100% (10/10) | 25% (1/4) | 50% (2/4) | 0% (0/1) |
| Stratum (one search per regulation) | 100% (10/10) | 100% (4/4) | 100% (4/4) | 100% (1/1) |

Misses by Stratum:

- `fh-setback-floor`: The answer correctly gives Fruit Heights' rule (setback set from the geological report, never less than 15 feet, and no habitable structure over a fault), but it also presents Table A-1 occupancy setbacks, the Batatian and Nelson calculation method and the Appendix B small-displacement exemption as Fruit Heights requirements, which the gold answer does not support and which appear to come from another jurisdiction's code (likely Draper).
- `fh-seismic-ground-motion`: The answer correctly gives Fruit Heights City's 2% in 50 years (2,500-year return period) requirement, but it says the city names the Salt Lake City segment of the Wasatch fault as controlling, which more plausibly belongs to Draper (Site A), since Fruit Heights lies near the Weber segment.
