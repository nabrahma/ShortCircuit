# Phase 26: Operational Strategy Manual
> **Last Updated:** Feb 04, 2026 | **Style:** Classical Trend Following (Sniper)
> **Status:** BATTLE TESTED (Feb 03)

## 1. Core Philosophy: "The Sniper"
We have shifted from high-frequency scalping to high-conviction trend following.
- **Goal:** Catch 1-2 major moves per day (e.g., Shorting extended stocks like `SBCL` at Day Highs).
- **Recent Win:** Correctly identified `SBCL` exhaustion at ₹486 (Day High) before it closed lower.
- **Safety:** Priorities Capital Preservation over signals.

## 2. The Filter Funnel (6 Gates)
Every signal must pass **6 Gates** before reaching you:

1.  **Market Regime (Context)**:
    - **Trend Up**: BLOCK Shorts.
    - **Trend Down**: BLOCK Longs.
    - **Range**: ALLOW Reversals (Our sweet spot).

2.  **Time of Day (Liquidity Protection)**:
    - **09:15 - 10:00**: CAUTION (High Volatility).
    - **12:00 - 13:00**: BLOCKED (Lunch time chop).
    - **Best Time**: 13:00 - 14:45 (Trend Confirmation).

3.  **Signal Cap (Discipline)**:
    - Max **5 Trades** per day.
    - **45-minute Cooldown** per stock.

4.  **HTF Confluence (The "Pro" Check)**:
    - **15-Minute Chart Alignment**:
    - We only short if 15m structure shows **Lower Highs** or **Exhaustion**.
    - If 15m is bullish, the 1m signal is ignored.

5.  **Technical Extension (Mean Reversion)**:
    - Price must be >2 SD from VWAP.
    - "Shorting the Rubber Band" when it's stretched.

6.  **Tape Reading (The Trigger)**:
    - **Absorption**: High Volume + No Price Move (Tape Stall).
    - **Orderbook (DOM)**: Bearish Wall (>2.5x Sellers vs Buyers).

## 3. Execution & Safety (New Phase 26 Features)
Once triggered, the Trade Manager takes over with "军 (Army) Grade" safety:

1.  **Dynamic Tick Size**:
    - Stops are calculated using the specific tick size of the stock (e.g., 0.05).
    - Prevents "Invalid Tick Size" rejection errors.

2.  **Smart Retry Logic**:
    - If SL Order fails, it retries **3 Times**.
    - If all 3 fail, it triggers **EMERGENCY EXIT** (Market Close) immediately.

3.  **Manual Fallback**:
    - If Auto-Trade is OFF, the bot calculates the levels but waits for you.
    - **Crash Fixed**: Handled specifically to prevent loop crashes.

## 4. Phase 27: Institutional Overlays (The "Pro" Layer)
We now look beyond Price. We look at **Participation (OI)** and **Value (Profile)**.

### A. OI Divergence (The "Fakeout" Detector)
- **Logic**: If Price breaks out (Highs), but Open Interest (OI) **DROPS**, it is a "Hollow Move" (Short Covering).
- **Signal**: `Price UP + OI DOWN` = **Aggressive Short**.
- *Note: Only active for F&O symbols.*

### B. Developing POC (Value Migration)
- **Logic**: We calculate the day's Volume Profile live.
- **Signal**: If Price makes a new High, but the **Point of Control (POC)** stays low (does not migrate up), it is a Value Divergence.
- **Trigger**: Price > POC + 1% = **Reversion Likely**.

### C. The "Vacuum" Test (Exhaustion)
- **Old Logic**: We only shorted High Volume rejection (Absorption).
- **New Logic (Phase 27)**: We now ALSO short **Low Volume Exhaustion**.
- **Trigger**: If Price is at +2SD VWAP and Volume **DROPS** (Vacuum), we enter. "No one is left to buy."

### C. The "Vacuum" Test (Exhaustion)
- **Concept**: Shorting "Low Volume Extensions".
- **Trigger**: If Price is at +2SD VWAP and Volume **DROPS** (Vacuum), allow entry.

## 5. Risk & Filters (Safe-Guards)

### A. The "Circuit Guard" (Anti-Trap)
To prevent getting locked in Upper Circuits (UC):
1.  **Gain Filter**: We ONLY trade stocks up **6% to 18%**.
    *   *< 6%*: Too weak (Noise).
    *   *> 18%*: Too dangerous (High risk of UC lock).
2.  **Dynamic Freeze Check**:
    *   The bot reads the real-time `UpperCircuit` limit from the exchange.
    *   If `LTP > (UpperCircuit * 0.985)` (i.e., within 1.5%), the trade is **BLOCKED**.

### B. Asset Classes
- **Primary**: Fyers Equity Intraday (NSE:EQ).
- **Focus**: High Volume, High Liquid stocks only.

---

# The Microstructure of the Turn (Original Thesis)

1. Introduction: The Anatomy of an Intraday Reversal
In the high-frequency domain of intraday trading, specifically within the volatility of the 1-minute timeframe, the concept of a "reversal" transcends the simplistic geometric patterns often taught in retail trading literature. To the institutional market participant—whether a proprietary desk scalper, a hedge fund execution trader, or a quantitative researcher—a reversal is not merely a change in price direction; it is a structural shift in the auction process. It represents a tangible transfer of inventory from aggressive participants to passive liquidity providers, a momentary failure of the market to facilitate trade at specific price extremes, and a re-evaluation of fair value by the collective market consciousness.

The 1-minute chart represents a chaotic intersection of noise and signal, a battleground where algorithmic execution logic, high-frequency trading (HFT) stops, and microstructure imbalances collide. At this level of granularity, price action is less about macroeconomic fundamentals and more about the immediate mechanics of supply and demand. Therefore, a "classic" reversal in this context is rarely defined by a single metric. Instead, it is a confluence of events: a rejection at a key reference level (such as a Value Area High or Point of Control), confirmed by an order flow anomaly (such as absorption or delta divergence), and executed against a backdrop of specific liquidity conditions in the order book.

Understanding these reversals requires a multi-dimensional approach. One cannot simply rely on a candlestick shape or a lagging indicator divergence. The professional trader synthesizes Auction Market Theory (AMT), which provides the context (where to trade); Order Flow analysis, which provides the confirmation (when to trade); and Depth of Market (DOM) liquidity analysis, which provides the execution logic (how to trade). This report provides an exhaustive examination of every technical and mechanical reversal signal available to the intraday trader, dissecting the anatomy of the turn from the perspective of market microstructure and detailing how professional desks leverage these tools to anticipate changes in direction before they become visible on the price chart.

1.1 The Fractal Nature of Market Reversals
Markets are fractal in nature, meaning that the patterns observed on a monthly or daily chart often repeat on smaller timeframes like the 1-minute chart, albeit with significantly more noise and faster expiry. A reversal on a 1-minute chart is often the "trigger" event for a larger rotation on a 5-minute or 15-minute chart. Professional traders understand that they are not trading the 1-minute chart in isolation; they are trading the microstructure reaction to macro-structure levels.

For instance, a reversal signal on a 1-minute chart (such as a volume spike and rejection) carries significantly more weight if it occurs at a Daily Point of Control (POC) or a Weekly Value Area Low (VAL). This concept of "confluence" is paramount. A 1-minute reversal signal in the middle of a trading range, far from any significant reference point, is often regarded as algorithmic noise or "churn." Conversely, the same signal occurring at a standard deviation extreme of the VWAP becomes a high-probability setup. Thus, the first step in identifying a reversal is not looking at the candle, but identifying the location of the trade.   

1.2 The Role of Algorithms and HFT
In modern markets, the vast majority of volume on the 1-minute timeframe is generated by algorithms. These automated systems are programmed to defend certain levels, hunt for liquidity, and execute mean-reversion strategies. A human trader looking for reversals is essentially attempting to identify the footprint of these algorithms.

For example, "spoofing" and liquidity layering in the order book are tactics used by HFTs to induce retail traders into a position before reversing the market. Understanding these manipulative behaviors is crucial. A "classic" chart pattern like a breakout can often be a trap set by algorithms to generate liquidity for a reversal. The professional trader uses tools like the Footprint chart and DOM to distinguish between a genuine breakout and a liquidity trap that is destined to reverse. This report will explore how to identify these "fakeouts" and "traps" which are essentially reversal setups in disguise.   

2. Classical Price Action and Indicator Confluence
While sophisticated order flow tools provide a significant edge, classical technical analysis remains the foundational language of the market. Algorithms are often programmed to recognize and react to standard candlestick patterns and indicator divergences. Therefore, these "classic" signals serve as self-fulfilling prophecies that traders must recognize. However, on a 1-minute chart, the interpretation of these signals requires a higher degree of nuance and strict filtering criteria to avoid the abundant false signals inherent in lower timeframes.

2.1 The Micro-Psychology of Reversal Candles
Candlestick patterns are visual representations of the battle between buyers and sellers within a specific time interval. On a 1-minute chart, specific candles signal the exhaustion of one side and the immediate counter-attack of the other.

2.1.1 The Doji and Spinning Top: Indecision vs. Transition
The Doji, characterized by a small body where the open and close are nearly identical, represents market indecision. On a 1-minute chart, a Doji appearing after a strong directional move is a primary alert signal. It indicates that the momentum that drove the price to that level has equalized with the opposing force.   

The Reversal Mechanism: Professional traders view the Doji not as a signal to enter, but as a signal to "arm" the trade. The reversal is confirmed only by the subsequent candle. If a green trend candle is followed by a Doji, and then a strong bearish candle, it forms an "Evening Star" pattern, signaling that the indecision was resolved in favor of the bears.   

Volume Filter: A Doji on low volume is often just a pause in the trend. A Doji on ultra-high volume is a profound reversal signal. It suggests that a massive amount of volume was transacted, yet price could not advance. This is a classic sign of "churn" or absorption, where heavy effort leads to no result, often preceding a sharp reversal.

2.1.2 The Hammer and Shooting Star (Pin Bars)
These are perhaps the most visually distinct reversal signals. A Hammer (at lows) or Shooting Star (at highs) features a long wick (shadow) and a small body.

The Liquidity Sweep: The long wick represents a "liquidity sweep" or a rejection. The market probed a level, triggered stops or found responsive limit orders, and was aggressively pushed back.   

Professional Interpretation: Pros analyze the location of the wick. A Shooting Star is only valid if the wick protrudes above a key resistance level (like the VAH or a Pivot Point). This indicates a "Look Above and Fail" scenario. If the wick is simply inside the previous range, it is ignored.

Order Flow Nuance: The most powerful Pin Bars are those where the wick contains "trapped" volume. If the Footprint chart shows heavy aggressive buying at the top of the Shooting Star's wick, it means traders bought the high and are now trapped. Their subsequent panic-selling will fuel the reversal.   

2.1.3 Engulfing Patterns and Marubozu
An Engulfing pattern occurs when a reversal candle completely overlaps the body of the previous candle. A Bullish Engulfing candle at a low signifies that buyers have overwhelmed sellers.

Momentum Shift: This pattern represents a "shock" to the system. The sentiment shifted from bearish to bullish within a single minute.

The Marubozu: A Marubozu is a candle with no wicks—just a solid body. A 1-minute Marubozu breaking out of a consolidation in the opposite direction of the trend is a "breakout reversal." It signals that one side is entering with maximum aggression, hitting market orders without hesitation.   

2.2 Indicator-Based Reversals: Divergence and Extremes
While price action tells the story of now, indicators provide the context of history. They measure the rate of change and statistical extremes relative to recent data.

2.2.1 RSI Divergence on the 1-Minute Chart
The Relative Strength Index (RSI) measures momentum. A classic reversal signal is "Divergence," where price makes a higher high, but the RSI makes a lower high.   

The Physics of Divergence: This signals deceleration. Imagine a car going up a hill; it is still moving up (higher price), but it is slowing down (lower RSI). Gravity (selling pressure) is about to take over.

The Scalper's Edge: On a 1-minute chart, RSI divergence alone is prone to failure during strong trends. Traders improve this signal by waiting for a "structure break." They identify the divergence, then wait for price to break the most recent 1-minute swing low before entering.   

Confluence with Bands: When RSI divergence occurs simultaneously with price hitting the Upper Bollinger Band, the probability of a reversal increases significantly. The Bollinger Band indicates statistical overextension (price is 2 standard deviations from the mean), while RSI indicates momentum exhaustion.   

2.2.2 MACD and Momentum Shifts
The Moving Average Convergence Divergence (MACD) is utilized to confirm shifts in trend direction. A "crossover" of the MACD line below the signal line, while above the zero line, serves as a bearish reversal signal.

Histogram Analysis: Intraday scalpers often focus on the MACD Histogram. A divergence in the histogram (where price pushes higher but the histogram bars shrink) is an early warning sign of momentum decay, often appearing before the classic line crossover.   

2.2.3 VWAP and Standard Deviation Reversions
The Volume Weighted Average Price (VWAP) is the most critical benchmark for institutional traders. It represents the "fair price" of the day based on volume.

Mean Reversion: Strategies often revolve around the concept that price cannot stay far from VWAP indefinitely. When price extends to the 2nd or 3rd Standard Deviation (SD) band of the VWAP, it is considered statistically overbought or oversold.

The "Rubber Band" Trade: A reversal setup is identified when price touches the +2 SD band. Traders look for a candlestick reversal pattern (like a Shooting Star) at the band. The trade targets a return to the VWAP (the mean). This is a pure mean-reversion strategy exploited by many algo-traders.   

Table 1: Classical 1-Minute Reversal Patterns & Filters
Pattern	Description	Professional Filter/Requirement
Doji	Small body, indecision.	Must occur after a strong trend; requires confirmation candle. High volume indicates absorption.
Hammer/Shooting Star	Long wick, small body.	Wick must reject a key level (VAH, POC, VWAP). "Trapped" volume in wick preferred.
Engulfing	Body covers previous candle.	Second candle volume must exceed first. Ideally closes near extreme (no wick).
RSI Divergence	Price High vs. RSI Low.	Must be combined with a structural break (break of swing low). Best at Bollinger/VWAP bands.
VWAP Extension	Price at +2/3 SD Band.	Do not fade strong trend days (Open Drive). Wait for consolidation/stalling at the band.
3. Auction Market Theory (AMT) and Profile Reversals
While classical analysis focuses on price and time, Auction Market Theory (AMT) focuses on price and volume. It views the market as a two-way auction process designed to facilitate trade. A reversal, in AMT terms, is the market's way of saying "Value has been found, and rejected."

3.1 The Value Area (VA) Reversal Dynamics
The Value Area represents the price range where 70% of the day's volume (Volume Profile) or TPOs (Market Profile) has occurred. This is the zone of "fair value" and acceptance. Reversals predominantly occur at the edges of this zone: the Value Area High (VAH) and Value Area Low (VAL).   

3.1.1 The "Look Above and Fail"
This is a high-probability reversal setup.

The Setup: The market rallies above the previous session's VAH or the current developing VAH. It attempts to explore higher prices to see if buyers are willing to transact there.

The Failure: The breakout lacks volume (exhaustion) or meets heavy selling (absorption). Price then falls back inside the Value Area.

The Reversal Trade: Once price closes back inside the VA on the 1-minute chart, traders initiate a short position. The theory dictates that once the "exploration" phase fails, the market will rotate back to the center of value (POC) or traverse the entire range to the VAL.

Target: The Point of Control (POC) or the opposite Value Area Low (VAL).   

3.1.2 The "Look Below and Fail"
The inverse of the above. Price breaks below the VAL, finds no sellers (or buyers step in aggressively), and reclaims the Value Area. This is often a sharp "V-shaped" reversal on the 1-minute chart, marking the low of the day.

3.2 The 80% Rule
The 80% Rule is a statistical tendency derived from Market Profile that is widely used by intraday traders to predict the extent of a reversal.

The Rule: If the market opens or moves outside the Value Area, and then re-enters and finds acceptance (trading there for two consecutive 30-minute TPO periods), there is an 80% probability that it will fill the entire Value Area to the other side.   

1-Minute Application: While the rule is based on 30-minute brackets, 1-minute scalpers use it to gauge the magnitude of a reversal. If a "Look Above and Fail" is confirmed, they don't just scalp for a few ticks; they hold for a move across the entire profile structure. They look for "acceptance" on the 1-minute chart, which looks like a flag or consolidation forming inside the VAH after the failed breakout.

3.3 Point of Control (POC) Mechanics
The POC is the price level with the highest volume. It acts as a massive gravitational anchor.

3.3.1 Virgin POC (Naked POC) Reversals
A "Virgin" or "Naked" POC (nPOC) is a POC from a previous day that has not yet been touched in the current session.

The Magnet Effect: As price approaches a nPOC, it often accelerates. However, because these levels represent historical high-value zones, they often act as stiff resistance or support upon the first test.

The Reversal: Professional traders leave limit orders at nPOC levels to fade the first touch. They expect a "reaction" or bounce, even if the level eventually breaks. It is a classic mean-reversion trade.   

3.3.2 Developing POC (dPOC) Migration
Traders watch the "Developing POC" throughout the day.

Signal: If price is making new highs on the 1-minute chart, but the dPOC does not move up (volume is not building at higher prices), it creates a divergence. This suggests the move is "hollow" or emotional, lacking broad participation.

Reversal: This divergence often precedes a sharp reversal back to the stationary POC, as the market realizes that value has not actually shifted higher.   

3.4 Profile Shapes and "Unfinished" Structures
The shape of the profile itself provides reversal clues.

P-Shape Profile: Indicates a short-covering rally (thin volume at lows, bulbous volume at highs). Reversals from the top of a P-shape are common if new buyers don't step in, as the short-covering fuel runs out.

b-Shape Profile: Indicates long liquidation (selling).

Poor Highs/Lows: A "Poor High" is a profile top with no "tail" (single prints). It looks flat, indicating that trade stopped at that exact level not because of a lack of buyers, but because of a mechanical limit (like an algorithm). AMT suggests markets revisit poor highs. Therefore, traders avoid shorting a poor high, as the market is likely to push through it before reversing.   

4. Order Flow Dynamics: The Footprint Chart
To confirm the hypotheses generated by Price Action and AMT, professional traders zoom into the atomic level of the market using Order Flow. The Footprint chart (or Volumetric chart) visualizes the actual volume traded at the Bid and the Ask for every price tick within the 1-minute candle. This x-ray view reveals the aggression of buyers and sellers.

4.1 Absorption: The "Brick Wall" Reversal
Absorption is one of the highest-probability signals in order flow trading. It occurs when aggressive market orders are absorbed by passive limit orders.

4.1.1 Identifying Absorption
The Setup: Price rallies to a resistance level (e.g., VAH).

The Signal: The Footprint chart shows massive buy volume (green numbers) at the high of the candle, yet the price cannot tick higher.

Interpretation: Aggressive buyers are hitting the "Ask," but a large seller (likely an iceberg order) is refreshing the offer, absorbing all the liquidity. The buyers are expending massive energy for zero result.

The Turn: Once the aggressive buyers exhaust themselves, they are "trapped." As price starts to tick down, these trapped buyers must sell to cover their losses, fueling the reversal downward.   

4.2 Exhaustion: The "Vacuum" Reversal
Exhaustion is the opposite of absorption. It is a reversal caused by a lack of participation.

The Signal: The market makes a new high on the 1-minute chart. However, the volume at this new high is extremely thin (e.g., only 50 contracts traded compared to 5,000 at the previous high).

Interpretation: Buyers have simply walked away. There is no one left to buy at these prices. The auction dries up.

The Turn: Price falls not because of heavy selling, but because of a lack of support. Gravity takes over. This often results in a "drift" reversal that accelerates as it finds lower liquidity.   

4.3 Delta Divergence: The Hidden Weakness
Delta is the net difference between Ask volume (aggressive buys) and Bid volume (aggressive sells).

Formula: Delta=Ask Volume−Bid Volume

Divergence Signal: A classic reversal signal occurs when Price makes a Higher High, but Delta makes a Lower High.

Meaning: Price is moving up, but with less aggressive buying force than before. The move is likely driven by thin liquidity or limit order pulling rather than genuine demand. This is a fragile state prone to collapse.   

The "Negative Delta" Up-Candle: A very specific and powerful reversal signal is a 1-minute candle that closes UP (Green) but has NEGATIVE Delta.

Meaning: Aggressive sellers pounded the bid (negative delta), yet price went up. This implies massive passive buying (limit orders) absorbed the selling and pushed price higher. It is a sign of hidden strength and often marks the exact bottom of a reversal.   

4.4 Unfinished Business (Failed Auctions)
In a "finished" auction, volume should taper off at the extreme (e.g., 0x100, then 0x50, then 0x0 at the very top).

The Anomaly: "Unfinished Business" occurs when the market reverses leaving volume at the extreme tick (e.g., 200 contracts traded at the absolute high).

Reversal Implication: Markets tend to revisit unfinished business. Therefore, if a 1-minute reversal leaves unfinished business, traders are cautious. They expect the market to come back, tag that level (finish the auction), and then reverse. A reversal pattern with a tapered (zero volume) top is considered much higher quality than one with unfinished business.   

5. Depth of Market (DOM) and Advanced Liquidity Analysis
While Footprint charts show what has happened (historical trades), the Depth of Market (DOM) shows what is waiting to happen (limit orders). This is the realm of "intent."

5.1 Liquidity Walls and Resistance
A "Liquidity Wall" is a visible cluster of large limit orders at a specific price.

The Reversal Play: If price approaches a massive sell wall (e.g., 1000 contracts on the Offer) and slows down, it suggests the wall is real. Traders watch for the "collision." If aggressive buying hits the wall and fails to break it (absorption), traders enter a short position, using the wall as a backstop for their stop-loss.

Risk: If the wall is "spoofed" (pulled before price hits it), the reversal thesis may fail. However, the removal of a wall can also create a "vacuum" that sucks price up before a reversal occurs at a higher level.   

5.2 Iceberg Orders
Icebergs are large limit orders that only display a small portion of their size.

Detection: Traders use DOM tools (like Jigsaw or Sierra Chart) or Heatmaps (Bookmap) to spot icebergs. They look for the "printing" of volume at a price where the liquidity size on the DOM does not decrease. (e.g., 500 contracts trade at the Bid, but the Bid size remains at 10).

Reversal Signal: An iceberg on the Bid at a support level is a bullish reversal signal. It indicates a large institutional player is "accumulating" or defending the level. Once the aggressive sellers realize they cannot push through the iceberg, they often panic-cover, driving a sharp reversal upwards.   

5.3 Liquidity Sweeps (Stop Hunts)
This is a predatory reversal pattern utilized by smart money.

Mechanism: Algorithms know where retail stop-losses are located (usually just below a visible swing low). They push price down through the low to trigger these stops.

The Event: The triggering of stop-loss sells creates a flood of liquidity. Institutions use this liquidity to fill their large Buy Limit orders.

The Reversal: On the 1-minute chart, this looks like a sharp spike down followed by an immediate reclaim of the level. This is often called a "Spring" (Wyckoff) or a "Swing Failure Pattern" (SFP).

Trade Setup: Traders wait for the candle to close back above the broken low. This confirms the sweep was a trap. They enter long, targeting the liquidity at the upper end of the range.   

5.4 Bookmap and Heatmap Visualizations
Modern pro traders use Heatmaps to visualize the historical evolution of the DOM.

Visualizing the Turn: A reversal often looks like a "fade" on the heatmap. A bright line (liquidity) appears above price (resistance). Price moves toward it, the line gets brighter (reinforcement), and aggressive buying bubbles (volume dots) appear but fail to penetrate the line. The reversal is confirmed when the aggressive buying dots stop (exhaustion) and price drifts away from the bright line.   

6. Integrated Reversal Strategies (The Playbook)
Professional traders do not use these tools in isolation. They combine them into specific "Playbooks" or setups. Here are the three most common integrated reversal strategies for the 1-minute chart.

Strategy 1: The "Trapped Trader" Reversal (Absorption Trap)
This strategy capitalizes on the emotional pain of traders caught on the wrong side of a breakout.

Context: Identify a key level (e.g., Daily High).

Trigger: Price breaks the high on the 1-minute chart.

Order Flow Signal: Delta is highly positive (aggressive buyers joining the breakout).

The Trap: Despite positive Delta, price stalls. Footprint shows massive volume at the top tick (Absorption). The DOM shows reloading offers (Iceberg).

Execution: Enter Short when the 1-minute candle closes back below the breakout level (or when the POC shifts lower).

Why it works: Breakout traders are trapped. Their stops are just below the breakout level. As price drops, their stops trigger, fueling the sell-off.   

Strategy 2: The "CVD Divergence" Fade
This strategy is used in grinding trends to identify the top.

Context: Market is trending up, but price action is overlapping and choppy.

Trigger: Price makes a new high.

Signal: Cumulative Volume Delta (CVD) makes a lower high. This shows that despite higher prices, buying pressure is evaporating.

Confirmation: Footprint shows "Exhaustion" (low volume) at the new high.

Execution: Enter Short on the first red candle or break of market structure (swing low).

Why it works: The trend has run out of fuel. It is "coasting" on inertia and will reverse at the first sign of selling.   

Strategy 3: The "VWAP Band" Mean Reversion
A statistical reversion trade.

Context: Price is extended to the +2 or +3 Standard Deviation band of the VWAP.

Trigger: Price touches the band.

Signal: A Reversal Candle (Shooting Star or Doji) forms at the band.

Order Flow: Negative Delta appears in the reversal candle, or a "failed auction" (unfinished business that is repaired and rejected).

Execution: Short with a target of the VWAP (Mean).

Why it works: Price is statistically overextended. Algorithms programmed for mean reversion will step in to fade the move.   

Table 2: 1-Minute Reversal Playbook Summary
Strategy Name	Context	Trigger	Confirmation	Stop Loss	Target
Trapped Trader	Breakout at Key Level (VAH/High)	Price fails to hold breakout	High Vol + Absorption + Delta Divergence	Above the Trap High	Range Low / POC
CVD Fade	Grinding/Weak Trend	New Price High	CVD Lower High + Low Volume (Exhaustion)	Above Swing High	Recent Swing Low
Liquidity Sweep	Test of Swing Low	Break of Low & Reclaim	Aggressive Delta Flip (Sellers trapped)	Below the Sweep Wick	Opposing Liquidity
VWAP Revert	+2/+3 SD Band Extension	Candle Rejection at Band	Negative Delta closing Green (Limit buying)	Above Band/High	VWAP (Mean)
7. Risk Management and Psychological Nuance
Trading reversals on the 1-minute chart is inherently dangerous. It is often described as "catching a falling knife" or "standing in front of a freight train." Professional survival depends on strict risk protocols that differ from retail conventions.

7.1 The Time Stop
In scalping, time is a risk factor. If a reversal trade does not work immediately, it is likely wrong.

The Rule: A valid reversal at a liquidity wall or absorption point should see a reaction within 1-3 minutes. If price hovers at the entry level for more than 3 candles, professionals often exit at breakeven ("scratch the trade"). They do not "hope" for the turn; they demand immediate feedback.   

7.2 Dynamic Position Sizing
Not all reversals are equal.

Structural Reversals: A reversal at a Daily nPOC with absorption is an "A+" setup. Pros may use full leverage.

Momentum Reversals: A simple RSI divergence in the middle of a range is a "C" setup. Pros may use 1/4 size or skip it entirely.

Confluence Scaling: The more factors align (e.g., VAH + VWAP Band + Absorption + Delta Divergence), the larger the position.

7.3 The "Trend Day" Danger
The biggest account killer for reversal traders is the "Trend Day" or "Double Distribution" day.

Identification: If the market opens and drives continuously in one direction with high volume and no overlapping ranges ("Open Drive"), reversal strategies must be turned off.

The mistake: Retail traders keep trying to short the top of a trend day, getting stopped out repeatedly. Pros recognize the profile shape (thin, elongated) and switch to trend-following (buying pullbacks) instead of fading extremes.   

7.4 Psychology of the Counter-Trend
Psychologically, reversal trading requires a contrarian mindset. You are selling when the candle looks most bullish (green and big) and buying when it looks most bearish.

Discomfort: Ideally, the trade should feel "scary." If you are selling into a massive green candle hitting a wall, it feels counter-intuitive. However, that is where the edge lies—providing liquidity to emotional chasers.

Discipline: The ability to take a small loss quickly is paramount. Because you are trading against momentum, if the level breaks, the move against you can be violent (a squeeze). Pros use "hard stops" in the system, never mental stops, for reversal trading.

8. Conclusion
The "classic" reversals on a 1-minute chart—candles, divergence, patterns—are merely the surface reflections of a deeper, more complex battle for liquidity. For the professional trader, these visual patterns are insufficient on their own. The edge lies in the confirmation provided by Order Flow and Auction Market Theory.

A true reversal is identified not when the chart looks like a turn, but when the structure of the auction breaks (Look Above and Fail), the liquidity in the book blocks the advance (Absorption/Icebergs), and the participation of the aggressors wanes (Delta/Volume Exhaustion). By synthesizing these data streams—seeing the "why" behind the "what"—the trader moves from gambling on chart shapes to executing probability-based strategies rooted in the physics of market microstructure. The 1-minute chart, often dismissed as noise, becomes a precise roadmap of institutional intent when viewed through this multi-dimensional lens.

