# How the sponsor fit scores are worked out

A plain-language guide. No maths background needed.

---

## The one-sentence version

Every local business gets a **fit score between 0 and 100**. It answers a single question: *how good a sponsorship prospect is this business for Dumbarton, all things considered?* Higher is better, and the queue is simply sorted from highest to lowest so the club starts at the top.

That number is built by scoring a business on **seven things**, giving each a weight (how much it matters), and adding them up:

> **fit score = community affinity + proximity + sector fit + brand fit + stability + community-mindedness + ability to pay**
> *(each worth a set number of points; they add up to 100)*

We **add** here (rather than multiply) because a sponsorship is a bundle of pros and cons, not a pass/fail gate — a business can be a bit weak on one thing and still be well worth a call. The weights are where the club's real priorities live, and they're all in one small config file, changeable without touching code.

---

## The big idea: affinity beats reach

Dumbarton averages a few hundred through the gate. The club's asset isn't *reach* — it's **local affinity**: the pull of a business being seen to back its own town's team. So the points are deliberately stacked that way:

| Feature | Points | What it measures |
|---|---:|---|
| **Community affinity** | 22 | How locally rooted / community-proud the business is |
| **Proximity** | 20 | How close it is to the ground |
| **Sector fit** | 15 | Does this *type* of business tend to sponsor clubs like us |
| **Brand fit** | 12 | Does backing a community club suit how they present themselves |
| **Company stability** | 11 | Are they solvent and actually trading |
| **Community-mindedness** | 10 | Visible charity / grassroots / local support |
| **Ability to pay** | 10 | Rough budget headroom — kept **light on purpose** |

Notice the two biggest numbers are community affinity and proximity (**42 of the 100 points together**), and raw size sits at the bottom. That single choice is the whole philosophy: a big national chain shouldn't outrank the local coach firm just because it's big.

---

## The four "hard" features (plain code, no AI)

These come straight from facts — a map, Companies House — so they're exact and need no judgement.

**Proximity.** Closer is better, and it drops off smoothly with distance. A business on the doorstep scores near the top; one across the county scores low. (A business ~1 km away keeps most of its 20 points; by ~6 km it's keeping under half.)

**Sector fit.** Some *kinds* of business sponsor lower-league clubs far more than others — coach firms, builders, pubs, local garages, funeral directors. Each sector has a propensity value in a table (coach/haulage and construction near the top; national chains and online-only retailers near the bottom). **Honesty note:** today this table is hand-written from what's typical in Scottish lower-league sponsorship. It's the one hand-guessed part of the score — and it's exactly what the planned *lookalike engine* will replace with a value **learned from who actually sponsors clubs like Dumbarton.**

**Company stability.** Older, established firms that are clearly still trading are safer bets, so stability rises with company age and saturates. If Companies House shows the accounts are dormant, this is cut sharply and the row is **flagged** to check they're still a going concern.

**Ability to pay.** A rough read from company size — micro, small, medium, large. It's in the score, but weighted **lightly on purpose**: we don't want budget headroom drowning out local fit, because a keen local firm with a modest budget is a better sponsor than a distant big one that doesn't care.

---

## The three "soft" features (the one place AI is used)

Some of what makes a good sponsor isn't a database field — it's *character*. Does this business read as genuinely rooted in the town? Would backing a football club actually suit them? Do they already show up for local causes? To read that, the tool shows an AI model the business's **own website text** and asks for three judgments: **community affinity**, **brand fit**, and **community-mindedness (CSR)**.

Two rules keep this honest and stop it inventing things:

- **It can only use the business's own words.** The model is told to judge *only* from the text provided and to **cite the fact behind each judgment**. It's not allowed to reward a business for community ties its website doesn't actually mention.
- **If it's unsure, its guess barely moves the score.** Every AI judgment comes with a **confidence**. A rich, clearly local website → high confidence → the judgment counts in full. A thin or missing website → low confidence → the judgment is pulled back toward the neutral middle, so a blank page neither helps nor hurts.

The AI never sees or sets the final score — it only supplies three of the seven inputs. The adding-up is done by plain, auditable code.

---

## Putting it together — three worked examples

**A local coach firm, 1 km away, 38 years old, family-run website.** Strong on everything that matters here:

> community 20.5 + proximity 16.4 + sector 13.5 + brand 10.8 + stability 11.0 + CSR 8.5 + ability 6.0 ≈ **87** → top of the queue.

**A national chain, 5.5 km away, large.** Full marks on size and stability — and it still can't climb:

> community 5.1 + proximity 8.0 + sector 3.0 + brand 2.3 + stability 11.0 + CSR 3.0 + ability 10.0 ≈ **42** → bottom third.

Its 21 points for size-and-stability are real, but the affinity-and-proximity block (where most of the points live) collapses — exactly as intended.

**A perfectly nice local firm with no website.** The three AI judgments can't be made, so they come back at **zero confidence** and get pulled to neutral:

> the soft features sit at the middle (≈ 22 points between them), the hard features score normally → a **middling score** and a *"no website found"* flag.

The tool refuses to invent a community story that isn't there — it parks the business in the middle and tells the human to go look.

---

## Two more things the queue gives you

**A suggested tier — a separate output, not part of the score.** Alongside the score, each business gets a suggested *type* of deal based on what it is: B2B firms → programme / website / training-wear; consumer-facing firms → matchday, hospitality, a visible board; community-facing firms → programme and community-naming bundles. It's a starting point for the conversation, not a price.

**Flags that travel with each row** — *"dormant accounts — check still trading," "national chain — decisions likely not local," "micro business — modest budget," "WARM PATH via…"* — so the reader knows where to be careful and where there's already a way in.

---

## What the score is — and isn't

- It's a **prioritisation aid**, not a verdict. It puts the most promising, most winnable local prospects at the top so a person spends their time on the right doors.
- **A person owns every contact.** The tool can't make the call, read the room, or close the deal — it ranks and prepares, then gets out of the way.
- **Every score breaks down into its reasons.** You can always see the seven point-contributions behind a number, and the exact sentence of the business's own website that drove each AI judgment. No black box.
- The numbers are **directional, not precise.** Treat an 87 vs an 84 as "both strong local prospects," not "one is clearly better."

In short: the score does the heavy lifting of comparing businesses fairly on the things that actually matter for a small club — and then it hands a prepared, prioritised list to a person to work.
