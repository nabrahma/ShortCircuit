import logging

# Logging Setup
logger = logging.getLogger("LiquidityMap")

class LiquidityHeatmap:
    def __init__(self):
        self.last_depth = None
        
    def detect_walls(self, depth_data):
        """
        Analyze Fyers Market Depth (Level 2).
        depth_data: {'bides': [{'price':, 'qty':}, ...], 'asks': [{'price':, 'qty':}, ...]}
        """
        if not depth_data: return None
        
        bids = depth_data.get('bids', [])
        asks = depth_data.get('asks', [])
        
        if not bids or not asks: return None
        
        # 1. Calculate Totals
        total_bid_qty = sum(item['qty'] for item in bids)
        total_ask_qty = sum(item['qty'] for item in asks)
        
        # 2. Imbalance Ratio
        # > 1.0 means More Asks (Selling Pressure)
        imbalance_ratio = total_ask_qty / total_bid_qty if total_bid_qty > 0 else 999.0
        
        # 3. Wall Detector (Single Order Dominance)
        # Is there ONE Ask level that has > 30% of Total Ask Qty?
        sell_wall_price = None
        for item in asks:
            if item['qty'] > (total_ask_qty * 0.30) and item['qty'] > 5000: # Min visible size
                sell_wall_price = item['price']
                break
                
        # 4. Result
        result = {
            'total_bid': total_bid_qty,
            'total_ask': total_ask_qty,
            'imbalance_ratio': round(imbalance_ratio, 2),
            'sell_wall': sell_wall_price,
            'is_bearish': imbalance_ratio > 2.0 or sell_wall_price is not None
        }
        
        return result
