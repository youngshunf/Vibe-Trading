class SignalEngine:
    """以 5/20 日均线生成可复现的全仓多头信号。"""

    def generate(self, data_map):
        """为每个真实行情序列生成目标仓位。"""
        signals = {}
        for code, frame in data_map.items():
            fast = frame["close"].rolling(5).mean()
            slow = frame["close"].rolling(20).mean()
            signals[code] = (fast > slow).astype(float)
        return signals
