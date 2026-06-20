# Protocol Search: Original vs. Expanded — Benchmark Results

Benchmark of Prof. Dennis Shasha's three test queries against the live protocols.io API, comparing the raw query against the concept-expansion pipeline (NCBI Taxonomy + Europe PMC term grounding, multi-signal re-rank).

## protocols.io search-syntax findings (empirical)

Probed directly against `/api/v3/protocols` (see `protocolsnerd-backend/probe_protocolsio_syntax.py`):

| Syntax | Supported? | Evidence |
|---|---|---|
| Single keyword | ✅ yes | `rice`→209, `CRISPR`→346, `multiplex`→400 |
| Adjacent 2-word phrase | ✅ only if the phrase occurs verbatim | `in planta`→10, `gene editing`→36, but `drought tolerance`→0 |
| Quotes `"..."` | ❌ matched literally, not as operator | `"drought tolerance"`→0 |
| `OR` / `AND` | ❌ matched literally | `drought OR rice`→0 |
| Parentheses | ❌ matched literally | `(drought OR water deficit) rice`→0 |
| Pagination | `page_id` is **0-indexed** | first page = `page_id=0` |

**Design consequence:** send many *short* single/adjacent-phrase probes and merge + re-rank client-side; do not use quotes/OR/parentheses.

## Summary: original vs. expanded

| Query | Raw query hits | Best protocol after expansion | Why it matches | Limitations |
|---|---|---|---|---|
| Find protocols that can allow in-planta tests in which genes | 0 | Agro Transformation for Mimulus in Planta Transformation | title matches ['planta']; method match (in planta); covers 1/1 concepts | raw query returns 0 (protocols.io needs short adjacent phrases); top hit lacks an explicit organism match |
| Find protocols that allow more than one transcription factor | 0 | Multiplex CRISPR genome regulation in mouse retina with hype | organism match (mouse); method match (multiplex, multiplex CRISPR); covers 2/2 concepts (A | raw query returns 0 (protocols.io needs short adjacent phrases) |
| Find protocols that test for drought tolerance in rice | 0 | A protocol for GC–MS-based metabolomic analysis in mature se | title matches ['rice']; organism match (rice, Oryza sativa); covers 1/2 concepts; synonym  | raw query returns 0 (protocols.io needs short adjacent phrases) |

## Query: Find protocols that can allow in-planta tests in which genes are modified

**Extracted concepts** — organisms: `[]`, methods: `['in planta']`, goals: `[]`, actions: `['modified', 'gene modification']`

**Concept expansion (grounded synonyms / related terms):**

| Concept | Expanded to | Source |
|---|---|---|
| in planta | plant transformation, agrobacterium, agroinfiltration, plant cells | Europe PMC / LLM / map |
| modified | genome editing, transgenic, CRISPR, modified bacon procedure | Europe PMC / LLM / map |
| gene modification | genome editing, transgenic, gene editing, genetic therapy | Europe PMC / LLM / map |

**Search probes fired** (`total_results` per probe): 
`in planta`→10, `plant transformation`→2, `agrobacterium`→61, `agroinfiltration`→2, `plant cells`→2, `genome editing`→81, `transgenic`→88, `CRISPR`→352, `gene editing`→36, `genetic therapy`→0

**Baseline (raw query):** `total_results=0` — no usable results

**Expanded results (top 5, multi-signal ranked):**

| # | Score | Protocol | Why it matches | Signals (T/O/M/D/S) |
|---|---|---|---|---|
| 1 | 5.833 | [Agro Transformation for Mimulus in Planta Transformatio](https://dx.doi.org/10.17504/protocols.io.3pagmie) | title matches ['planta']; method match (in planta); covers 1/1 concepts | 1.0/0.0/1.5/0.333/0.0 |
| 2 | 5.833 | [Agro Preparation for Mimulus in Planta Transformation](https://dx.doi.org/10.17504/protocols.io.3rqgm5w) | title matches ['planta']; method match (in planta); covers 1/1 concepts | 1.0/0.0/1.5/0.333/0.0 |
| 3 | 5.833 | [Plant Infiltration for Mimulus in Planta Transformation](https://dx.doi.org/10.17504/protocols.io.3rtgm6n) | title matches ['planta']; method match (in planta); covers 1/1 concepts | 1.0/0.0/1.5/0.333/0.0 |
| 4 | 5.5 | [Preparation of botanical extracts for in vitro and in p](https://dx.doi.org/10.17504/protocols.io.5qpvoezndl4o/v1) | title matches ['planta']; method match (in planta); covers 1/1 concepts | 1.0/0.0/1.5/0.0/0.0 |
| 5 | 5.5 | [Mimulus in planta Transformation](https://dx.doi.org/10.17504/protocols.io.3tkgnkw) | title matches ['planta']; method match (in planta); covers 1/1 concepts | 1.0/0.0/1.5/0.0/0.0 |

**Limitations:** raw query returns 0 (protocols.io needs short adjacent phrases); top hit lacks an explicit organism match

## Query: Find protocols that allow more than one transcription factor to be modified at the same time for mice

**Extracted concepts** — organisms: `['mice']`, methods: `['transcription factor', 'multiplex']`, goals: `[]`, actions: `['modified']`

**Concept expansion (grounded synonyms / related terms):**

| Concept | Expanded to | Source |
|---|---|---|
| mice | Mus sp., Mus musculus, mouse | NCBI Taxonomy |
| transcription factor | transcriptional regulator, DNA-binding, transcription factors | Europe PMC / LLM / map |
| multiplex | multiplex CRISPR, multiplexed, combinatorial | Europe PMC / LLM / map |
| modified | genome editing, transgenic, CRISPR, modified bacon procedure | Europe PMC / LLM / map |

**Search probes fired** (`total_results` per probe): 
`Mus sp.`→0, `Mus musculus`→9, `mouse`→1198, `mice`→640, `transcription factor`→147, `transcriptional regulator`→15, `DNA-binding`→4, `transcription factors`→26, `multiplex`→401, `multiplex CRISPR`→2, `multiplexed`→185, `combinatorial`→40, `genome editing`→81, `transgenic`→88

**Baseline (raw query):** `total_results=0` — no usable results

**Expanded results (top 5, multi-signal ranked):**

| # | Score | Protocol | Why it matches | Signals (T/O/M/D/S) |
|---|---|---|---|---|
| 1 | 9.5 | [Multiplex CRISPR genome regulation in mouse retina with](https://dx.doi.org/10.21203/rs.3.pex-1811/v1) | organism match (mouse); method match (multiplex, multiplex CRISPR); covers 2/2 c | 0.0/2.0/1.5/0.0/1.0 |
| 2 | 9.25 | [Multiplexed immunofluorescence and RNA-FISH ](https://dx.doi.org/10.17504/protocols.io.yxmvm9zk9l3p/v1) | organism match (mice); method match (multiplexed); covers 2/2 concepts (ALL); sy | 0.0/2.0/1.5/0.25/0.5 |
| 3 | 6.0 | [Modified DAP-seq protocol using a high-yield wheat germ](https://dx.doi.org/10.17504/protocols.io.bp2l6j54kvqe/v1) | title matches ['transcription', 'modified']; method match (transcription factor, | 1.5/0.0/1.5/0.75/0.5 |
| 4 | 5.5 | [Protocol to accompany paper entitled "Alternative splic](https://dx.doi.org/10.17504/protocols.io.pnidmce) | title matches ['transcription', 'factor']; method match (transcription factor, t | 1.5/0.0/1.5/0.5/0.5 |
| 5 | 5.25 | [Phenotyping mice for Prox1-eGFP transgene expression](https://dx.doi.org/10.17504/protocols.io.dm6gp74m8gzp/v1) | title matches ['mice']; organism match (mice, mouse); covers 1/2 concepts; synon | 0.75/2.0/0.0/0.5/0.5 |

**Limitations:** raw query returns 0 (protocols.io needs short adjacent phrases)

## Query: Find protocols that test for drought tolerance in rice

**Extracted concepts** — organisms: `['rice']`, methods: `[]`, goals: `['drought', 'tolerance', 'drought tolerance']`, actions: `[]`

**Concept expansion (grounded synonyms / related terms):**

| Concept | Expanded to | Source |
|---|---|---|
| rice | Oryza sativa, Asian cultivated rice | NCBI Taxonomy |
| drought | drought stress, water deficit, water stress, droughts | Europe PMC / LLM / map |
| tolerance | resistance, stress response, drug tolerance | Europe PMC / LLM / map |
| drought tolerance | drought stress, water deficit, drought resistance, droughts | Europe PMC / LLM / map |

**Search probes fired** (`total_results` per probe): 
`Oryza sativa`→3, `rice`→209, `drought stress`→2, `water deficit`→0, `water stress`→1, `droughts`→0, `drought`→13, `resistance`→256, `stress response`→13, `drug tolerance`→0, `tolerance`→53, `drought resistance`→0, `drought tolerance`→0

**Baseline (raw query):** `total_results=0` — no usable results

**Expanded results (top 5, multi-signal ranked):**

| # | Score | Protocol | Why it matches | Signals (T/O/M/D/S) |
|---|---|---|---|---|
| 1 | 5.333 | [A protocol for GC–MS-based metabolomic analysis in matu](https://dx.doi.org/10.1038/protex.2017.151) | title matches ['rice']; organism match (rice, Oryza sativa); covers 1/2 concepts | 1.0/2.0/0.0/0.333/0.5 |
| 2 | 5.0 | [Expression and Purification of the Rice Clock protein, ](https://dx.doi.org/10.17504/protocols.io.261gekmb7g47/v1) | title matches ['rice']; organism match (rice, Oryza sativa); covers 1/2 concepts | 1.0/2.0/0.0/0.0/0.5 |
| 3 | 4.833 | [Construction of high-quality rice ribosome footprint li](https://dx.doi.org/10.17504/protocols.io.2ktgcwn) | title matches ['rice']; organism match (rice); covers 1/2 concepts | 1.0/2.0/0.0/0.333/0.0 |
| 4 | 4.833 | [Expression and Purification of Rice SUMOylation machine](https://dx.doi.org/10.17504/protocols.io.5qpvoe6edl4o/v1) | title matches ['rice']; organism match (rice); covers 1/2 concepts | 1.0/2.0/0.0/0.333/0.0 |
| 5 | 3.833 | [An efficient regeneration and transformation protocol f](https://dx.doi.org/10.17504/protocols.io.rm7vz4pm8lx1/v1) | organism match (rice); covers 1/2 concepts | 0.0/2.0/0.0/0.333/0.0 |

**Limitations:** raw query returns 0 (protocols.io needs short adjacent phrases)
