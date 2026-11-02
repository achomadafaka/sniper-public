from __future__ import annotations

from typing import Dict, List, Optional

from .base import StrategyPlugin
from .fade_v1 import FadeV1Strategy
from .insider_copy_v1 import InsiderCopyV1Strategy
from .crypto_momentum_v1 import CryptoMomentumV1Strategy
from .oil_momentum_v1 import OilMomentumV1Strategy
from .sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
from .weather_v1 import WeatherV1Strategy
from .sports_no_value_v1 import SportsNoValueV1Strategy
from .cross_arb_v1 import CrossArbV1Strategy
from .political_no_value_v1 import PoliticalNoValueV1Strategy
from .news_intel_v1 import NewsIntelV1Strategy
from .term_structure_v1 import TermStructureV1Strategy
from .crowd_fade_v1 import CrowdFadeV1Strategy
from .hl_liquidation_v1 import HlLiquidationV1Strategy
from .stink_bid_v1 import StinkBidV1Strategy
from .shadow_copy_v1 import ShadowCopyV1Strategy
from .source_position_copy_v1 import SourcePositionCopyV1Strategy


_PLUGINS: Dict[str, StrategyPlugin] = {
    InsiderCopyV1Strategy.plugin_id: InsiderCopyV1Strategy(),
    FadeV1Strategy.plugin_id: FadeV1Strategy(),
    WeatherV1Strategy.plugin_id: WeatherV1Strategy(),
    OilMomentumV1Strategy.plugin_id: OilMomentumV1Strategy(),
    CryptoMomentumV1Strategy.plugin_id: CryptoMomentumV1Strategy(),
    SportsLineDislocationV1Strategy.plugin_id: SportsLineDislocationV1Strategy(),
    SportsNoValueV1Strategy.plugin_id: SportsNoValueV1Strategy(),
    CrossArbV1Strategy.plugin_id: CrossArbV1Strategy(),
    PoliticalNoValueV1Strategy.plugin_id: PoliticalNoValueV1Strategy(),
    NewsIntelV1Strategy.plugin_id: NewsIntelV1Strategy(),
    TermStructureV1Strategy.plugin_id: TermStructureV1Strategy(),
    CrowdFadeV1Strategy.plugin_id: CrowdFadeV1Strategy(),
    HlLiquidationV1Strategy.plugin_id: HlLiquidationV1Strategy(),
    StinkBidV1Strategy.plugin_id: StinkBidV1Strategy(),
    ShadowCopyV1Strategy.plugin_id: ShadowCopyV1Strategy(),
    SourcePositionCopyV1Strategy.plugin_id: SourcePositionCopyV1Strategy(),
}


def get_strategy_plugin(strategy_type: str) -> Optional[StrategyPlugin]:
    return _PLUGINS.get(str(strategy_type or "").strip().lower())


def list_strategy_plugins() -> List[StrategyPlugin]:
    return list(_PLUGINS.values())
