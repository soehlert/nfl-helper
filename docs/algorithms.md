# Core Mathematical & Deterministic Algorithms

This document details all algorithmic logic, mathematical formulas, edge-case handling, and step-by-step procedures powering the **Craftroom Draftroom** engine. All logic is strictly deterministic (zero external LLM/cloud dependencies).

---

## 1. Snake Draft Lookahead & Turn Gap Math

### Objective
In a snake draft with $N$ teams and $R$ total rounds, accurately determine:
1. Which draft slot owns any arbitrary pick number $P$.
2. The exact number of picks remaining until the user's turn.
3. The subsequent turn gap (number of opponent picks between the user's upcoming pick and their next turn).
4. Whether the user is currently "on the clock".

### Formula & Logic
For overall pick $P \ge 1$ in an $N$-team league:
1. **Round Number**:
   $$R = \lfloor\frac{P - 1}{N}\rfloor + 1$$
2. **Pick within Round** (1-indexed, $1 \le K \le N$):
   $$K = ((P - 1) \pmod N) + 1$$
3. **Draft Slot Owner** ($1 \le S \le N$):
   $$\text{Owner}(P) = \begin{cases} K & \text{if } R \text{ is odd (Round 1, 3, 5\dots)} \\ N - K + 1 & \text{if } R \text{ is even (Round 2, 4, 6\dots)} \end{cases}$$

### Lookahead Distance
For a user assigned to draft slot $U \in [1, N]$:
- Let the list of all pick numbers assigned to $U$ across all $R_{total}$ rounds be $\mathcal{S}_U = [p_1, p_2, \dots, p_{R_{total}}]$.
- If current pick $P \in \mathcal{S}_U$:
  - `is_on_the_clock = True`
  - `picks_until_user_turn = 0`
  - Next pick is $P$ itself ($p_{curr} = P$).
  - Subsequent pick is the next entry in $\mathcal{S}_U$, denoted $p_{next}$.
  - `snake_turn_gap` $= p_{next} - P - 1$.
- If current pick $P \notin \mathcal{S}_U$:
  - `is_on_the_clock = False`
  - Find the smallest $p_{next} \in \mathcal{S}_U$ such that $p_{next} > P$.
  - `picks_until_user_turn` $= p_{next} - P$.
  - Find the subsequent pick $p_{sub} \in \mathcal{S}_U$ such that $p_{sub} > p_{next}$.
  - `snake_turn_gap` $= p_{sub} - p_{next} - 1$.

### Edge Cases & Properties
- **Slot 1 (The 1st Turn)**:
  - Round 1 Pick 1 $\to$ Round 2 Pick 12 (overall pick 24 in 12-team). Gap $= 24 - 1 - 1 = 22$ picks.
  - Round 2 Pick 12 (overall 24) $\to$ Round 3 Pick 1 (overall 25). Gap $= 25 - 24 - 1 = 0$ picks (back-to-back pick).
- **Slot 12 (The Turnaround)**:
  - Round 1 Pick 12 (overall 12) $\to$ Round 2 Pick 1 (overall 13). Gap $= 13 - 12 - 1 = 0$ picks.
  - Round 2 Pick 1 (overall 13) $\to$ Round 3 Pick 12 (overall 36). Gap $= 36 - 13 - 1 = 22$ picks.
- **Middle Slots (e.g. Slot 6 in 12-team)**:
  - Round 1 Pick 6 $\to$ Round 2 Pick 7 (overall 19). Gap $= 19 - 6 - 1 = 12$ picks ($2(N - U)$).
  - Round 2 Pick 7 (overall 19) $\to$ Round 3 Pick 6 (overall 30). Gap $= 30 - 19 - 1 = 10$ picks ($2(U - 1)$).

---

## 2. Positional Tier Clustering

### Objective
Cluster available players by position (QB, RB, WR, TE, K, D/ST) into discrete tiers to measure positional scarcity and identify drop-off cliffs.

### Method 1: Cheatsheet-Blended Clustering (Priority)
When a user uploads a cheatsheet containing explicit tier designations:
1. Group available players by `player.cheatsheet_tier` (or fallback to mathematical clustering if not specified).
2. Sort tiers ascending ($1, 2, 3\dots$).
3. Compute `avg_projected = mean(p.projected_points for p in tier_players)` and `count = len(tier_players)`.

### Method 2: Statistical Drop-Off Clustering (Algorithmic)
When no cheatsheet is provided or when evaluating unranked players:
1. Sort available players for position descending by `projected_points`: $[x_1, x_2, \dots, x_M]$.
2. Compute consecutive point drop-offs: $\Delta_i = x_i - x_{i+1}$.
3. A new tier boundary is triggered at index $i+1$ if either:
   - **Significant Single Drop**: $\Delta_i \ge \tau_{single}(pos)$
     - QB: $1.8$ pts, RB: $1.4$ pts, WR: $1.4$ pts, TE: $1.8$ pts, K/DST: $1.2$ pts.
   - **Cumulative Span Limit**: $(x_{\text{tier\_max}} - x_{i+1}) \ge \tau_{span}(pos)$
     - QB: $3.5$ pts, RB: $2.8$ pts, WR: $2.8$ pts, TE: $3.5$ pts, K/DST: $2.0$ pts.
4. Calculate `next_tier_drop_points` for Tier $T$ as:
   $$\text{Drop}(T) = \text{avg\_projected}(T) - \text{avg\_projected}(T+1)$$

---

## 3. Three-Scenario Tier Cliff Detection

### Objective
Alert the user when an active tier for a critical skill position (QB, RB, WR, TE) is in imminent danger of complete depletion before or during their upcoming turn windows.

### Scenarios & Dynamic Scarcity Triggers
Let $C$ be the number of players remaining in the highest active tier, $S_{tier}$ be the total size of that tier, $P_{wait}$ be `picks_until_user_turn`, $G$ be `snake_turn_gap`, and $\text{Drop}$ be the point loss to the next tier ($\text{Drop} \ge 1.0$ pts required).

#### 1. `ON_THE_CLOCK_CLIFF` (On the Clock Scarcity)
- **When**: User is drafting right now ($P_{wait} \le 0$).
- **Trigger**: $\frac{C}{S_{tier}} \le 0.30$ (less than $30\%$ of tier remains) OR $C \le \max(2, \lfloor\frac{G+2}{3}\rfloor)$.
- **Meaning**: You are on the clock. If you pass on this position now, the remaining $C$ players in this tier will be taken before your next pick in $G$ turns.
- **Action**: Draft this position now to avoid dropping $-\text{Drop}$ pts to the next tier.

#### 2. `UPCOMING_TURN_CLIFF` (Upcoming Turn Scarcity)
- **When**: User is waiting for their pick ($P_{wait} > 0$).
- **Trigger**: $C > P_{wait}$ AND $C \le (P_{wait} + \max(2, \lfloor\frac{G+2}{3}\rfloor))$ AND $\frac{C}{S_{tier}} \le 0.40$ AND $G \ge 6$.
- **Meaning**: The tier will survive until your upcoming turn, but after you pick, the subsequent turn gap ($G$ picks) will wipe out whatever is left.
- **Action**: Prepare to target this position at your upcoming turn.

#### 3. `DEPLETED_BEFORE_TURN` (Depletion Ahead of Turn)
- **When**: User is waiting for their pick ($P_{wait} > 0$).
- **Trigger**: ($C \le 2$ OR $\frac{C}{S_{tier}} \le 0.25$) AND $P_{wait} \ge \max(1, C)$.
- **Meaning**: Opponents drafting ahead of you are mathematically expected to drain the remaining $C$ players before you even reach the clock.
- **Action**: Do not bank on drafting this tier; prepare to target Tier $T+1$ or pivot to another position.

---

## 4. VORP & Draft Suggestions with Platform ADP Arbitrage

### VORP (Value Over Replacement Player)
VORP quantifies a player's weekly advantage over the waiver/baseline starter pool:

1. **Starter Baseline Determination**:
   For an $N$-team league with standard roster slots ($1\text{ QB}, 2\text{ RB}, 3\text{ WR}, 1\text{ TE}, 1\text{ FLEX}, 1\text{ K}, 1\text{ D/ST}$):
   - Baseline Rank QB: $N \times 1 = 12$
   - Baseline Rank RB: $N \times 2.5 = 30$ (accounting for flex share)
   - Baseline Rank WR: $N \times 3.5 = 42$ (accounting for flex share)
   - Baseline Rank TE: $N \times 1.2 = 14$
   - Baseline Rank K: $N \times 1 = 12$
   - Baseline Rank D/ST: $N \times 1 = 12$
2. **Positional Baseline Score ($B_{pos}$)**:
   The projected points of the player at the baseline rank for that position.
   > **Note**: Baselines $B_{pos}$ are precomputed and cached in-memory once per draft setup.
3. **Player VORP**:
   $$\text{VORP}(p) = \max(0, \text{projected\_points}(p) - B_{\text{pos}(p)})$$

### Platform ADP Market Arbitrage
Draft consensus ADP (from ESPN or Sleeper) reflects actual opponent tendencies:
$$\text{ADP Delta}(p) = P_{\text{overall}} - \text{ADP}(p)$$
- **$\text{ADP Delta} > 0$ ("Market Steal / Fall")**: Player is still available past their consensus ADP. Opponents may pick them any moment.
- **$\text{ADP Delta} < 0$ ("Reach")**: Drafting ahead of consensus ADP. If player is in a safe tier with low cliff risk, drafting can be safely deferred.

### Suggestion Score Ranking
$$\text{Score}(p) = \text{VORP}(p) + \text{CliffBonus}(p) + \text{CheatsheetBonus}(p) + \text{ADPDeltaWeight}(p)$$
- $\text{CliffBonus}$: $+3.0$ pts if position has `ON_THE_CLOCK_CLIFF`, $+1.5$ pts for `UPCOMING_TURN_CLIFF`.
- $\text{CheatsheetBonus}$: $+2.0$ pts if player is in Cheatsheet Tier 1, $+1.0$ pt for Tier 2.
- Suggestions are sorted descending by $\text{Score}$, returning the top 5 ranked tactical recommendations.
