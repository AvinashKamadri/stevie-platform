# Round 1 — Review Aid (PROPOSAL — nothing recorded)

Generated 2026-07-02 to accelerate your Phase 1 review of the 100
uncertainty-ranked pairs (`gold/active_queue_v1.2.jsonl`). **These are
recommendations, not labels.** No decision has been written to
`organization_merge_decision` or any gold file. You remain the oracle: label via
`stevie review --lane main` (applying/adjusting these), or tell me to record a
set you've approved.

Schema: **merge** = same entity · **distinct** = different entities · **related**
= real relationship but not the same (parent/subsidiary, brand/product, group/
member). `related` is reported, never trained as a positive.

---

## The finding that matters most

**~40 of the 100 pairs are the same pattern: same brand, different country/
region.** The model scores them p≈0.500 because it has *no geographic signal* —
this is a genuine information ceiling (the geography analogue of the acronym
ceiling in M5), not per-pair ambiguity. Labeling them one-by-one without a rule
will be inconsistent, which corrupts the experiment. **Two policy decisions
resolve most of the queue at once:**

- **P1 — Same brand, different country → `merge` or `distinct`?**
  e.g. *DHL Express Ghana* vs *DHL Express Lima*; *Weber Shandwick Belgium* vs
  *…Netherlands*; *Nu Skin Enterprises Hong Kong* vs *…Philippines*; *SM
  Supermalls (China)* vs *SM Supermalls (Philippine city)*; *NEC Corporation
  India* vs *…of America*; *Tata Consultancy Services Asia Pacific* vs
  *…Philippines*. My read: these are **distinct** award entrants (separate
  regional entities). If your canonical model is strictly brand-level, they're
  **merge**. Pick one rule and it settles ~30 pairs.

- **P2 — Parent/subsidiary & brand/product → `related` or `distinct`?**
  e.g. *Telkom Indonesia* vs *Admedika – Telkom Indonesia Group*; *Paysafe* vs
  *Income Access (a Paysafe Company)*; *Bosch Group* vs *Bosch Thermotechnology
  NA*; *RiverSource Insurance* vs *RiverSource Life*; *Amyris* vs *Pipette and
  Purecane (Amyris Brands)*. My read: **related**.

Same-country, different-**site/office** of one legal entity is a clean **merge**
regardless of P1/P2 (e.g. *NCR Corporation Dayton* vs *…Duluth*; *MSW … Knoxville*
vs *…Washington DC*).

---

## Bucket A — recommend MERGE (same entity: formatting / typo / legal suffix / acronym / site)

High confidence unless noted.

| Pair | Why |
|---|---|
| BlueCat Networks Inc, Toronto Ontario / …Toronto ON Canada | punctuation only |
| TBWA Group Istanbul / TBWA\Istanbul | same office |
| Llorente y Cuenca / LLYC, Llorente y Cuenca | LLYC *is* the acronym |
| APPA / Australasian Promotional Products Association | acronym expansion |
| BMC, Houston / BMC Software, Houston | "Software" dropped |
| McGallen and Bolden / McGallen & Bolden Group | and/& + "Group" |
| Blackboard, Inc. Washington / Blackboard, Washington DC | legal suffix + location fmt |
| Fannie Mae / FannieMae | spacing |
| Indian Oil / IndianOil | spacing |
| PT Unilever Indonesia, Tbk. / Unilever Indonesia | legal prefix/suffix |
| HANAROADCOM, 하나로애드컴 / 하나로애드컴 | latin+hangul vs hangul |
| Dell, Inc., Round Rock / Dell, Round Rock | legal suffix |
| NCR Corporation, Dayton / …Duluth, GA | same corp, diff site (P1-independent) |
| MSW Interactive Designs LLC, Knoxville / …Washington DC | same LLC, diff office |
| Wells Fargo Bank – Treasury Mgmt Client Services / Wells Fargo Treasury Mgmt Client Delivery | same team, renamed |
| Design studio IONOI / Ionoi Studio | word order |
| Dimes Gida / Dimes Sanayi ve Ticaret A.Ş. | brand vs full legal name (same co, Türkiye) |
| AstraZeneca Kazakhstan / Representative Office "AstraZeneca UK Ltd" in Kazakhstan | *likely* same local entity — **verify** |

## Bucket B — recommend DISTINCT (different entities; token overlap is coincidental)

| Pair | Why |
|---|---|
| McCain Consulting / McCain Foods | consulting vs food co, diff countries |
| Rainbow Chicken (ZA) / Rainbow Communication (KR) | unrelated |
| Extreme Networks / Extreme Reach | different companies |
| Chorus.ai / Chorus One | different companies (US vs CH) |
| Transcend Academy / Transcend Technologies | different companies |
| International Business Machine / International SOS | unrelated |
| International Cybernetics (ICC) / International SOS | unrelated |
| A Closer Look / Look | unrelated |
| Harri / Harris | different companies |
| Merck & / Mercku | pharma vs CA hardware startup |
| PRONE (KR) / Pronet (TR) | unrelated |
| Mercedes-Benz S-Class Launch / MSL | a campaign vs an agency |
| Hunter Plastic Surgery (AU) / The Surgery (UK) | unrelated |
| Ogilvy Public Relations Istanbul / Public Relations | one is a generic fragment |
| Mission 4 Sight, Cloquet / Sight | unrelated |
| Acuity / HR Acuity | different companies |
| Atomic 212 (AU) / Atomic PR (US) | different agencies |
| Velocity Consulting (AU) / Velocity Global (US) | different companies |
| Oasis Brand Communications (HK) / OASIS CADDE (TR) | unrelated |
| Access Brand Communications / Access Communications | *probably* distinct — **verify** |
| XO Group / XO Marketing Group | *probably* distinct — **verify** |
| Newlink Communications / Newlink Corporate | *probably* related/same — **verify** |
| Nuance Comms + Gold Coast Health / Nuance Comms + Mackay Hospital | two different joint entries |
| Jacob & / Jacobs | *likely* distinct — **verify** (truncation artifact?) |
| Mint / United States Mint – Washington DC | *possibly* same — **verify** ("Mint" alone) |
| Domino's Pizza, Istanbul / Domino's Pizza Türkiye | *possibly* same national entity — **verify** |

## Bucket C — recommend RELATED (real link, not the same entity)

RiverSource Insurance/Life · Telkom Indonesia / Admedika–Telkom Indonesia Group ·
Paysafe / Income Access (a Paysafe Company) · Amyris / Pipette and Purecane
(Amyris Brands) · Bosch Group / Bosch Thermotechnology NA · Imperial Brands /
Imperial Tobacco Canada · Heinz / Kraft Heinz · John Hancock Funds / …Investments ·
Mohawk Industries / The Mohawk Group · Blue Cross Blue Shield of Florida / Blue
Cross of Idaho · Compass CHC of Barnstaple / Compass Group · LLORENTE & CUENCA /
LLORENTE Y CUENCA MADRID SL · Novartis Consumer Health / Edelman.ergo (joint
entry) · ringzwei / HOFFMANN UND CAMPE Corporate Publishing (joint entry) ·
Curriculum Advantage / Curriculum Associates · Travelport Digital / Travelport
Locomote · Reed Elsevier NV Amsterdam / Reed Elsevier Philippines · Yalla Ludo /
Yalla Technology FZ-LLC.

## Bucket D — the P1 policy cluster (same brand, different country) — my default: DISTINCT

All the **DHL Express** cross-country pairs (Ghana/Lima/India/MENA/Kenya/
Switzerland/Argentina/Egypt/Philippines/Qatar/Chile/Saudi Arabia/Isando/Rwanda/
Uganda — ~23 pairs) · **Weber Shandwick** Belgium/Germany/Netherlands · **Nu Skin
Enterprises** Singapore/SE-Asia and HK/Philippines · **SM Supermalls (China)** vs
each Philippine SM City · **SM Shopping Center** Chengdu/Tianjin · **NEC
Corporation** India/America · **Tata Consultancy Services** Asia-Pacific/
Philippines · **MetLife** Asia/China.

→ Under a **decide-once P1 rule** these all resolve together. Flag if any specific
one should differ.

---

## How to proceed

1. **Decide P1 and P2** (two rules) — this is the real unblock.
2. Skim Buckets A–C; the `**verify**` rows are the ~8 that genuinely need your eyes.
3. Then either label in `stevie review` yourself, or approve a set and I'll record
   it with honest provenance (`source=active_learning`, noting AI-assisted draft).

Expected shape after P1=distinct / P2=related: roughly **merge 18 · distinct 55 ·
related 19 · verify 8** — i.e. this hard-case round is mostly *negatives*, exactly
what a boundary-sharpening round should be.
