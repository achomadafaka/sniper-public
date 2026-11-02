"""Tests for Polymarket client data fetching."""

import unittest
from unittest.mock import Mock, patch, MagicMock
from data.polymarket_client import PolymarketClient, PricePoint


class TestPolymarketClient(unittest.TestCase):
    
    def setUp(self):
        self.client = PolymarketClient()
    
    @patch('data.polymarket_client.requests.Session.get')
    def test_get_market_meta_success(self, mock_get):
        """Test successful market metadata fetch."""
        mock_response = Mock()
        mock_response.json.return_value = [{
            'conditionId': '0x123',
            'outcomes': ['YES', 'NO'],
            'clobTokenIds': ['token_yes', 'token_no']
        }]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response
        
        result = self.client.get_market_meta('0x123')
        
        self.assertIsNotNone(result)
        self.assertEqual(result['condition_id'], '0x123')
        self.assertEqual(result['outcomes'], ['YES', 'NO'])
        self.assertEqual(result['outcome_to_token']['YES'], 'token_yes')
        self.assertEqual(result['outcome_to_token']['NO'], 'token_no')
    
    @patch('data.polymarket_client.requests.Session.get')
    def test_get_market_meta_not_found(self, mock_get):
        """Test market metadata fetch failure."""
        mock_response = Mock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response
        
        result = self.client.get_market_meta('0xnonexistent')
        
        self.assertIsNone(result)
    
    @patch('data.polymarket_client.requests.Session.get')
    def test_get_clob_snapshot_success(self, mock_get):
        """Test successful price snapshot fetch."""
        mock_response = Mock()
        mock_response.json.return_value = {
            'history': [
                {'t': 1000, 'p': '0.65'},
                {'t': 1060, 'p': '0.66'},  # 60s from target
                {'t': 1120, 'p': '0.67'}
            ]
        }
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response
        
        result = self.client._get_clob_snapshot(
            'token_yes', target_ts=1000, start_ts=900, end_ts=1200, max_gap_seconds=300
        )
        
        self.assertIsNotNone(result)
        self.assertEqual(result.price, 0.65)
        self.assertEqual(result.matched_ts, 1000)
        self.assertEqual(result.lag_seconds, 0)
        self.assertEqual(result.source, 'clob_prices_history')
        self.assertEqual(result.confidence, 'high')
    
    @patch('data.polymarket_client.requests.Session.get')
    def test_get_clob_snapshot_too_far(self, mock_get):
        """Test snapshot fetch when nearest point is too far."""
        mock_response = Mock()
        mock_response.json.return_value = {
            'history': [
                {'t': 500, 'p': '0.60'},  # 500s from target (too far)
            ]
        }
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response
        
        result = self.client._get_clob_snapshot(
            'token_yes', target_ts=1000, start_ts=400, end_ts=600, max_gap_seconds=300
        )
        
        self.assertIsNone(result)  # Too far from target
    
    @patch('data.polymarket_client.requests.Session.get')
    def test_get_trade_proxy_price_success(self, mock_get):
        """Test trade proxy fallback."""
        mock_response = Mock()
        mock_response.json.return_value = [
            {
                'outcome': 'YES',
                'price': 0.64,
                'timestamp': 1005  # seconds, 5s from target
            }
        ]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response
        
        result = self.client._get_trade_proxy_price(
            '0xmarket', 'YES', timestamp=1000, max_gap_seconds=300
        )
        
        self.assertIsNotNone(result)
        self.assertEqual(result.price, 0.64)
        self.assertEqual(result.source, 'data_trades_proxy')
        self.assertEqual(result.confidence, 'low')
        self.assertEqual(result.noise_band_pct, 5.0)
        self.assertFalse(result.validated)
    
    @patch('data.polymarket_client.requests.Session.get')
    def test_get_trade_proxy_price_no_match(self, mock_get):
        """Test trade proxy when no matching trade found."""
        mock_response = Mock()
        mock_response.json.return_value = [
            {'outcome': 'NO', 'price': 0.35, 'timestamp': 1000000}  # Wrong outcome
        ]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response
        
        result = self.client._get_trade_proxy_price(
            '0xmarket', 'YES', timestamp=1000, max_gap_seconds=300
        )
        
        self.assertIsNone(result.price)
        self.assertEqual(result.source, 'data_trades_proxy')
        self.assertFalse(result.validated)
        self.assertIn('No matching trade', result.note)
    
    @patch.object(PolymarketClient, 'get_market_meta')
    @patch.object(PolymarketClient, '_get_clob_snapshot')
    def test_get_price_at_uses_snapshot(self, mock_snapshot, mock_meta):
        """Test that snapshot is used when available."""
        mock_meta.return_value = {
            'outcome_to_token': {'YES': 'token_yes'}
        }
        mock_snapshot.return_value = PricePoint(
            price=0.65,
            matched_ts=1000,
            lag_seconds=0,
            source='clob_prices_history',
            confidence='high',
            noise_band_pct=None,
            validated=True,
            note='Snapshot'
        )
        
        result = self.client.get_price_at('0xmarket', 'YES', 1000)
        
        self.assertEqual(result.source, 'clob_prices_history')
        self.assertEqual(result.confidence, 'high')
        self.assertTrue(result.validated)
    
    @patch.object(PolymarketClient, 'get_market_meta')
    @patch.object(PolymarketClient, '_get_clob_snapshot')
    @patch.object(PolymarketClient, '_get_trade_proxy_price')
    def test_get_price_at_fallback_to_trades(self, mock_proxy, mock_snapshot, mock_meta):
        """Test fallback to trades when snapshot unavailable."""
        mock_meta.return_value = {
            'outcome_to_token': {'YES': 'token_yes'}
        }
        mock_snapshot.return_value = None  # Snapshot failed
        mock_proxy.return_value = PricePoint(
            price=0.64,
            matched_ts=1005,
            lag_seconds=5,
            source='data_trades_proxy',
            confidence='low',
            noise_band_pct=5.0,
            validated=False,
            note='Fallback'
        )
        
        result = self.client.get_price_at('0xmarket', 'YES', 1000)
        
        self.assertEqual(result.source, 'data_trades_proxy')
        self.assertEqual(result.confidence, 'low')
        self.assertFalse(result.validated)


if __name__ == '__main__':
    unittest.main()
