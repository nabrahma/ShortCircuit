import pandas as pd
import numpy as np
import logging

# Logging Setup
logger = logging.getLogger("MarketProfile")

class ProfileAnalyzer:
    def __init__(self):
        pass
        
    def calculate_tpo_profile(self, df, price_step=None):
        """
        Calculates a TPO-like profile using 1-minute time blocks.
        Each 1-min candle contributes to the "Time at Price" count.
        """
        if df is None or df.empty: return None
        
        try:
            # 1. Determine Price Bins (Tick Size)
            min_p = df['low'].min()
            max_p = df['high'].max()
            
            # Auto-calc step (e.g. 0.05 or based on range)
            if price_step is None:
                price_range = max_p - min_p
                # If range is small, use 0.05. If large (Type 20000), use 1.0.
                if price_range < 5: price_step = 0.05
                elif price_range < 100: price_step = 0.10
                elif price_range < 1000: price_step = 0.50
                else: price_step = 1.0
            
            # Create Bins
            # We want bins covering Min to Max
            bins = np.arange(min_p, max_p + price_step, price_step)
            
            # 2. Build TPO Counts (Time Distribution)
            # Iterate through candles and increment count for all bins touched by High-Low range.
            # This is "True" TPO (Range coverage).
            
            # Faster Vectorized approach?
            # Creating a histogram for every candle is slow.
            # Pivot approach:
            
            # Initialize TPO map
            tpo_counts = np.zeros(len(bins) - 1)
            
            # Loop (optimization: vectorized binning not trivial for ranges)
            # Approximation: Use Close price for simplified TPO, or Low/High/Close.
            # "Pro" TPO uses the full range.
            
            # Let's start with a simpler yet effective approach:
            # Count closes in bins (Time at Price)
            # This is effectively a Frequency Distribution of time.
            
            # Using 'close' as the TPO marker
            counts, bin_edges = np.histogram(df['close'], bins=bins)
            
            # 3. Calculate Value Area (70%)
            total_tpo = counts.sum()
            limit = total_tpo * 0.70
            
            # Find POC (Mode)
            poc_idx = np.argmax(counts)
            poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx+1]) / 2
            
            # Value Area Algorithm: Start at POC and expand out
            # We expand up/down greedily or symmetrically? 
            # Standard Market Profile expands by adding the larger neighboring TPO count.
            
            current_tpo = counts[poc_idx]
            up_idx = poc_idx + 1
            dn_idx = poc_idx - 1
            
            # Bounds
            low_bound = dn_idx
            high_bound = up_idx
            
            while current_tpo < limit:
                # Check Bounds
                can_go_up = up_idx < len(counts)
                can_go_down = dn_idx >= 0
                
                if not can_go_up and not can_go_down:
                    break
                    
                up_val = counts[up_idx] if can_go_up else -1
                dn_val = counts[dn_idx] if can_go_down else -1
                
                # Decision
                # 1. If we can't go one way, force the other
                if not can_go_down:
                    current_tpo += up_val
                    high_bound = up_idx
                    up_idx += 1
                elif not can_go_up:
                    current_tpo += dn_val
                    low_bound = dn_idx
                    dn_idx -= 1
                # 2. Compare Values (Prefer UP on ties to ensure progress)
                elif up_val >= dn_val:
                    current_tpo += up_val
                    high_bound = up_idx
                    up_idx += 1
                else:
                    current_tpo += dn_val
                    low_bound = dn_idx
                    dn_idx -= 1
                    
            val = bin_edges[max(0, low_bound)]
            vah = bin_edges[min(len(bin_edges)-1, high_bound + 1)] # +1 to get upper edge
            
            return {
                'poc': poc_price,
                'vah': vah,
                'val': val,
                'counts': counts,
                'bins': bin_edges,
                'total_tpo': total_tpo
            }
            
        except Exception as e:
            logger.error(f"TPO Calc Error: {e}")
            return None

    def check_profile_rejection(self, df, ltp):
        """
        Signal: "Look Above and Fail"
        Price breaks VAH but closes back inside.
        """
        # We need Context (Profile of the DAY so far)
        # Assuming df contains today's data.
        
        # Calculate Profile excluding the last few candles (Developing Struct)?
        # No, usually we trade against the Developing Structure of the day.
        
        profile = self.calculate_tpo_profile(df)
        if not profile: return False, "Profile Error"
        
        vah = profile['vah']
        poc = profile['poc']
        
        # Logic:
        # Check last 3 candles for the "Probe & Fail" pattern.
        if len(df) < 5: return False, "No Data"
        
        # Setup: One of the recent candles High > VAH
        # Trigger: Current Candle Close < VAH
        
        recent = df.iloc[-3:]
        
        # Did we probe above VAH?
        poked_above = recent['high'].max() > vah
        
        # Are we currently below VAH (and notably below, not just noise)?
        # Buffer: 0.05% below VAH
        curr_close = df.iloc[-1]['close']
        buffer = vah * 0.9995
        
        closed_back_in = curr_close < buffer
        
        # Additional Filter: The 'Fail' should be sharp (Drift logic handles the slow ones)
        # If we poked above, expecting expansion. If we close back in, it's a trap.
        
        if poked_above and closed_back_in:
            # Confirm it's not just a downtrend staying below VAH.
            # We must have been ABOVE VAH recently.
            
            # Count closes above VAH in recent history (Acceptance Check)
            # If we accepted above VAH for too long (> 30 mins), VAH might migrate up. 
            # But here we assume fixed/developing profile.
            
            return True, f"Look Above & Fail (VAH: {vah:.2f})"
        
        return False, f"Inside VA (VAH: {vah:.2f})"

    def check_single_prints(self, df, ltp):
        """
        Detects 'Single Prints' (Thin Liquidity / Vacuum Zones).
        """
        # Logic: Fast price movement with low TPO counts.
        # This acts as a target (Price magnets to fill singles) or Rejection (if created at top).
        
        # Simplified: Large range candle with low volume relative to day?
        pass
