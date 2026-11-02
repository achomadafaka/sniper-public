"""Deployed V3 swap ABIs; Pancake adds two trailing protocol-fee words."""
import re

from eth_utils import keccak

V3_SWAP = '0x' + keccak(b'Swap(address,address,int256,int256,uint160,uint128,int24)').hex()
PANCAKE_V3_SWAP = '0x' + keccak(b'Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)').hex()


def valid_pancake_v3_data(data):
    """Seven complete ABI words, including the otherwise unused uint128 fee tail."""
    if not isinstance(data, str) or not re.fullmatch(r'0x[0-9a-fA-F]{448}', data):
        return False
    words = [int(data[i:i + 64], 16) for i in range(2, len(data), 64)]
    tick = words[4] - (1 << 256) if words[4] >= 1 << 255 else words[4]
    return (words[2] < 1 << 160 and words[3] < 1 << 128
            and -(1 << 23) <= tick < 1 << 23
            and words[5] < 1 << 128 and words[6] < 1 << 128)
