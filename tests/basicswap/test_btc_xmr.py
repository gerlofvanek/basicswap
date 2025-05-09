#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2021-2024 tecnovert
# Copyright (c) 2024-2025 The Basicswap developers
# Distributed under the MIT software license, see the accompanying
# file LICENSE or http://www.opensource.org/licenses/mit-license.php.

import random
import logging
import unittest

from basicswap.basicswap import (
    BidStates,
    Coins,
    DebugTypes,
    SwapTypes,
)
from basicswap.basicswap_util import (
    TxLockTypes,
    EventLogTypes,
)
from basicswap.db import (
    Concepts,
)
from basicswap.util import (
    make_int,
)
from basicswap.util.address import (
    decodeAddress,
)
from basicswap.util.extkey import ExtKeyPair
from basicswap.interface.base import Curves
from tests.basicswap.util import (
    read_json_api,
)
from tests.basicswap.common import (
    abandon_all_swaps,
    wait_for_balance,
    wait_for_bid,
    wait_for_event,
    wait_for_offer,
    wait_for_unspent,
    wait_for_none_active,
    BTC_BASE_RPC_PORT,
)
from basicswap.contrib.test_framework.descriptors import descsum_create
from basicswap.contrib.test_framework.messages import (
    from_hex,
    CTxIn,
    COutPoint,
    CTransaction,
    CTxInWitness,
)
from basicswap.contrib.test_framework.script import (
    CScript,
    OP_EQUAL,
    OP_CHECKLOCKTIMEVERIFY,
    OP_CHECKSEQUENCEVERIFY,
)
from .test_xmr import BaseTest, test_delay_event, callnoderpc

logger = logging.getLogger()
test_seed = "8e54a313e6df8918df6d758fafdbf127a115175fdd2238d0e908dd8093c9ac3b"


class TestFunctions(BaseTest):
    base_rpc_port = None

    node_a_id = 0
    node_b_id = 1
    node_c_id = 2

    def callnoderpc(self, method, params=[], wallet=None, node_id=0):
        return callnoderpc(node_id, method, params, wallet, self.base_rpc_port)

    def mineBlock(self, num_blocks=1):
        self.callnoderpc("generatetoaddress", [num_blocks, self.btc_addr])

    def check_softfork_active(self, feature_name):
        deploymentinfo = self.callnoderpc("getdeploymentinfo")
        assert deploymentinfo["deployments"][feature_name]["active"] is True

    def getBalance(self, js_wallets, coin) -> float:
        if coin == Coins.PART_BLIND:
            coin_ticker: str = "PART"
            balance_type: str = "blind_balance"
            unconfirmed_name: str = "blind_unconfirmed"
        elif coin == Coins.PART_ANON:
            coin_ticker: str = "PART"
            balance_type: str = "anon_balance"
            unconfirmed_name: str = "anon_pending"
        elif coin == Coins.NAV:
            coin_wallet = js_wallets[coin.name]
            return (
                float(coin_wallet["balance"])
                + float(coin_wallet["unconfirmed"])
                + float(coin_wallet["immature"])
            )
        else:
            coin_ticker: str = coin.name
            balance_type: str = "balance"
            unconfirmed_name: str = "unconfirmed"

        return float(js_wallets[coin_ticker][balance_type]) + float(
            js_wallets[coin_ticker][unconfirmed_name]
        )

    def do_test_01_full_swap(self, coin_from: Coins, coin_to: Coins) -> None:
        logging.info("---------- Test {} to {}".format(coin_from.name, coin_to.name))

        # Offerer sends the offer
        # Bidder sends the bid
        id_offerer: int = self.node_a_id
        id_bidder: int = self.node_b_id

        swap_clients = self.swap_clients
        reverse_bid: bool = swap_clients[0].is_reverse_ads_bid(coin_from, coin_to)
        ci_from = swap_clients[id_offerer].ci(coin_from)
        ci_to = swap_clients[id_bidder].ci(coin_to)
        ci_part0 = swap_clients[id_offerer].ci(Coins.PART)
        ci_part1 = swap_clients[id_bidder].ci(Coins.PART)

        self.prepare_balance(
            coin_from, 100.0, 1800 + id_offerer, 1801 if reverse_bid else 1800
        )

        # Leader sends the initial (chain a) lock tx.
        # Follower sends the participate (chain b) lock tx.
        id_leader: int = id_bidder if reverse_bid else id_offerer
        id_follower: int = id_offerer if reverse_bid else id_bidder
        logging.info(
            f"Offerer, bidder, leader, follower: {id_offerer}, {id_bidder}, {id_leader}, {id_follower}"
        )

        # js_0 = read_json_api(1800 + id_offerer, 'wallets')
        # node0_from_before: float = self.getBalance(js_0, coin_from)

        js_1 = read_json_api(1800 + id_bidder, "wallets")
        node1_from_before: float = self.getBalance(js_1, coin_from)

        node0_sent_messages_before: int = ci_part0.rpc(
            "smsgoutbox",
            [
                "count",
            ],
        )["num_messages"]
        node1_sent_messages_before: int = ci_part1.rpc(
            "smsgoutbox",
            [
                "count",
            ],
        )["num_messages"]

        amt_swap = ci_from.make_int(random.uniform(0.1, 2.0), r=1)
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)
        offer_id = swap_clients[id_offerer].postOffer(
            coin_from, coin_to, amt_swap, rate_swap, amt_swap, SwapTypes.XMR_SWAP
        )
        wait_for_offer(test_delay_event, swap_clients[id_bidder], offer_id)
        offer = swap_clients[id_bidder].listOffers(filters={"offer_id": offer_id})[0]
        assert offer.offer_id == offer_id

        post_json = {"with_extra_info": True}
        offer0 = read_json_api(
            1800 + id_offerer, f"offers/{offer_id.hex()}", post_json
        )[0]
        offer1 = read_json_api(
            1800 + id_offerer, f"offers/{offer_id.hex()}", post_json
        )[0]
        assert "lock_time_1" in offer0
        assert "lock_time_1" in offer1

        bid_id = swap_clients[id_bidder].postXmrBid(offer_id, offer.amount_from)

        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            BidStates.BID_RECEIVED,
            wait_for=(self.extra_wait_time + 40),
        )

        bid0 = read_json_api(1800 + id_offerer, f"bids/{bid_id.hex()}")
        bid1 = read_json_api(1800 + id_bidder, f"bids/{bid_id.hex()}")

        tolerance = 2
        assert bid0["ticker_from"] == ci_from.ticker()
        assert bid1["ticker_from"] == ci_from.ticker()
        assert bid0["ticker_to"] == ci_to.ticker()
        assert bid1["ticker_to"] == ci_to.ticker()
        assert abs(ci_from.make_int(bid0["amt_from"]) - amt_swap) <= tolerance
        assert abs(ci_from.make_int(bid1["amt_from"]) - amt_swap) <= tolerance
        assert abs(ci_to.make_int(bid0["bid_rate"]) - rate_swap) <= tolerance
        assert abs(ci_to.make_int(bid1["bid_rate"]) - rate_swap) <= tolerance
        assert bid0["reverse_bid"] == reverse_bid
        assert bid1["reverse_bid"] == reverse_bid

        found: bool = False
        bids0 = read_json_api(1800 + id_offerer, "bids")
        for bid in bids0:
            if bid["bid_id"] != bid_id.hex():
                continue
            assert bid["amount_from"] == bid1["amt_from"]
            assert bid["bid_rate"] == bid1["bid_rate"]
            found = True
            break
        assert found

        swap_clients[id_offerer].acceptBid(bid_id)

        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            BidStates.SWAP_COMPLETED,
            wait_for=(self.extra_wait_time + 180),
        )
        wait_for_bid(
            test_delay_event,
            swap_clients[id_bidder],
            bid_id,
            BidStates.SWAP_COMPLETED,
            sent=True,
            wait_for=(self.extra_wait_time + 30),
        )

        amount_from = float(ci_from.format_amount(amt_swap))
        js_1_after = read_json_api(1800 + id_bidder, "wallets")
        node1_from_after = self.getBalance(js_1_after, coin_from)
        if coin_from is not Coins.PART:  # TODO: staking
            assert node1_from_after > node1_from_before + (amount_from - 0.05)

        # TODO: Discard block rewards
        # js_0_after = read_json_api(1800 + id_offerer, 'wallets')
        # node0_from_after: float = self.getBalance(js_0_after, coin_from)
        # assert (node0_from_after < node0_from_before - amount_from)

        scale_from = ci_from.exp()
        amount_to = int((amt_swap * rate_swap) // (10**scale_from))
        amount_to_float = float(ci_to.format_amount(amount_to))
        node1_to_after: float = self.getBalance(js_1_after, coin_to)
        node1_to_before: float = self.getBalance(js_1, coin_to)
        if False:  # TODO: set stakeaddress and xmr rewards to non wallet addresses
            assert node1_to_after < node1_to_before - amount_to_float

        node0_sent_messages_after: int = ci_part0.rpc(
            "smsgoutbox",
            [
                "count",
            ],
        )["num_messages"]
        node1_sent_messages_after: int = ci_part1.rpc(
            "smsgoutbox",
            [
                "count",
            ],
        )["num_messages"]
        node0_sent_messages: int = (
            node0_sent_messages_after - node0_sent_messages_before
        )
        node1_sent_messages: int = (
            node1_sent_messages_after - node1_sent_messages_before
        )
        split_msgs: int = (
            2
            if (
                ci_from.curve_type() != Curves.secp256k1
                or ci_to.curve_type() != Curves.secp256k1
            )
            else 0
        )
        assert node0_sent_messages == (
            3 + split_msgs if reverse_bid else 4 + split_msgs
        )
        assert node1_sent_messages == (
            4 + split_msgs if reverse_bid else 2 + split_msgs
        )

        post_json = {"show_extra": True}
        bid0 = read_json_api(1800 + id_offerer, f"bids/{bid_id.hex()}", post_json)
        bid1 = read_json_api(1800 + id_bidder, f"bids/{bid_id.hex()}", post_json)

        chain_a_lock_txid = None
        chain_b_lock_txid = None
        for tx in bid0["txns"]:
            if tx["type"] == "Chain A Lock Spend":
                chain_a_lock_txid = tx["txid"]
            elif tx["type"] == "Chain B Lock Spend":
                chain_b_lock_txid = tx["txid"]
        for tx in bid1["txns"]:
            if not chain_a_lock_txid and tx["type"] == "Chain A Lock Spend":
                chain_a_lock_txid = tx["txid"]
            elif not chain_b_lock_txid and tx["type"] == "Chain B Lock Spend":
                chain_b_lock_txid = tx["txid"]
        assert chain_a_lock_txid is not None
        assert chain_b_lock_txid is not None

    def do_test_02_leader_recover_a_lock_tx(
        self, coin_from: Coins, coin_to: Coins, lock_value: int = 32
    ) -> None:
        logging.info(
            "---------- Test {} to {} leader recovers coin a lock tx".format(
                coin_from.name, coin_to.name
            )
        )

        id_offerer: int = self.node_a_id
        id_bidder: int = self.node_b_id

        swap_clients = self.swap_clients
        reverse_bid: bool = swap_clients[0].is_reverse_ads_bid(coin_from, coin_to)
        ci_from = swap_clients[id_offerer].ci(coin_from)
        ci_to = swap_clients[id_offerer].ci(coin_to)

        id_leader: int = id_bidder if reverse_bid else id_offerer
        id_follower: int = id_offerer if reverse_bid else id_bidder
        logging.info(
            f"Offerer, bidder, leader, follower: {id_offerer}, {id_bidder}, {id_leader}, {id_follower}"
        )

        # js_wl_before = read_json_api(1800 + id_leader, 'wallets')
        # wl_from_before = self.getBalance(js_wl_before, coin_from)

        amt_swap = ci_from.make_int(random.uniform(0.1, 2.0), r=1)
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)
        offer_id = swap_clients[id_offerer].postOffer(
            coin_from,
            coin_to,
            amt_swap,
            rate_swap,
            amt_swap,
            SwapTypes.XMR_SWAP,
            lock_type=TxLockTypes.SEQUENCE_LOCK_BLOCKS,
            lock_value=lock_value,
        )
        wait_for_offer(test_delay_event, swap_clients[id_bidder], offer_id)
        offer = swap_clients[id_bidder].getOffer(offer_id)

        bid_id = swap_clients[id_bidder].postXmrBid(offer_id, offer.amount_from)
        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            BidStates.BID_RECEIVED,
            wait_for=(self.extra_wait_time + 40),
        )

        swap_clients[id_follower].setBidDebugInd(
            bid_id, DebugTypes.BID_STOP_AFTER_COIN_A_LOCK
        )
        swap_clients[id_offerer].acceptBid(bid_id)

        leader_sent_bid: bool = True if reverse_bid else False
        wait_for_bid(
            test_delay_event,
            swap_clients[id_leader],
            bid_id,
            BidStates.XMR_SWAP_FAILED_REFUNDED,
            sent=leader_sent_bid,
            wait_for=(self.extra_wait_time + 180),
        )
        wait_for_bid(
            test_delay_event,
            swap_clients[id_follower],
            bid_id,
            [BidStates.BID_STALLED_FOR_TEST, BidStates.XMR_SWAP_FAILED],
            sent=(not leader_sent_bid),
            wait_for=(self.extra_wait_time + 30),
        )

        # TODO: Discard block rewards
        # js_wl_after = read_json_api(1800 + id_leader, 'wallets')
        # wl_from_after = self.getBalance(js_wl_after, coin_from)
        # assert (node0_from_before - node0_from_after < 0.02)

    def do_test_03_follower_recover_a_lock_tx(
        self, coin_from, coin_to, lock_value: int = 32, with_mercy: bool = False
    ):
        logging.info(
            "---------- Test {} to {} follower recovers coin a lock tx{}".format(
                coin_from.name, coin_to.name, " (with mercy tx)" if with_mercy else ""
            )
        )

        # Leader is too slow to recover the coin a lock tx and follower swipes it
        # Coin B lock tx remains unspent unless a mercy output revealing the follower's keyshare is sent

        id_offerer: int = self.node_a_id
        id_bidder: int = self.node_b_id

        abandon_all_swaps(test_delay_event, self.swap_clients[id_offerer])
        abandon_all_swaps(test_delay_event, self.swap_clients[id_bidder])

        swap_clients = self.swap_clients
        reverse_bid: bool = swap_clients[0].is_reverse_ads_bid(coin_from, coin_to)
        ci_from = swap_clients[id_offerer].ci(coin_from)
        ci_to = swap_clients[id_offerer].ci(coin_to)

        id_leader: int = id_bidder if reverse_bid else id_offerer
        id_follower: int = id_offerer if reverse_bid else id_bidder
        logging.info(
            f"Offerer, bidder, leader, follower: {id_offerer}, {id_bidder}, {id_leader}, {id_follower}"
        )

        swap_clients[id_follower].ci(
            coin_to if reverse_bid else coin_from
        )._altruistic = with_mercy

        amt_swap = ci_from.make_int(random.uniform(0.1, 2.0), r=1)
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)
        offer_id = swap_clients[id_offerer].postOffer(
            coin_from,
            coin_to,
            amt_swap,
            rate_swap,
            amt_swap,
            SwapTypes.XMR_SWAP,
            lock_type=TxLockTypes.SEQUENCE_LOCK_BLOCKS,
            lock_value=lock_value,
        )
        wait_for_offer(test_delay_event, swap_clients[id_bidder], offer_id)
        offer = swap_clients[id_bidder].getOffer(offer_id)

        bid_id = swap_clients[id_bidder].postXmrBid(offer_id, offer.amount_from)
        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            BidStates.BID_RECEIVED,
            wait_for=(self.extra_wait_time + 40),
        )

        swap_clients[id_leader].setBidDebugInd(
            bid_id, DebugTypes.BID_DONT_SPEND_COIN_A_LOCK_REFUND2
        )
        debug_type = DebugTypes.BID_DONT_SPEND_COIN_B_LOCK
        swap_clients[id_follower].setBidDebugInd(bid_id, debug_type)

        swap_clients[id_leader].setBidDebugInd(
            bid_id, DebugTypes.WAIT_FOR_COIN_B_LOCK_BEFORE_REFUND, False
        )
        swap_clients[id_follower].setBidDebugInd(
            bid_id, DebugTypes.WAIT_FOR_COIN_B_LOCK_BEFORE_REFUND, False
        )

        swap_clients[id_offerer].acceptBid(bid_id)

        leader_sent_bid: bool = True if reverse_bid else False

        expect_state = (
            (BidStates.XMR_SWAP_NOSCRIPT_TX_REDEEMED, BidStates.SWAP_COMPLETED)
            if with_mercy
            else (BidStates.BID_STALLED_FOR_TEST, BidStates.XMR_SWAP_FAILED_SWIPED)
        )
        wait_for_bid(
            test_delay_event,
            swap_clients[id_leader],
            bid_id,
            expect_state,
            wait_for=(self.extra_wait_time + 180),
            sent=leader_sent_bid,
        )
        wait_for_bid(
            test_delay_event,
            swap_clients[id_follower],
            bid_id,
            BidStates.XMR_SWAP_FAILED_SWIPED,
            wait_for=(self.extra_wait_time + 80),
            sent=(not leader_sent_bid),
        )

        # TODO: Exclude block rewards
        # js_w1_after = read_json_api(1800 + id_bidder, 'wallets')
        # node1_from_before = self.getBalance(js_w1_before, coin_from)
        # node1_from_after = self.getBalance(js_w1_after, coin_from)
        # amount_from = float(format_amount(amt_swap, 8))
        # assert (node1_from_after - node1_from_before > (amount_from - 0.02))

        swap_clients[id_offerer].abandonBid(bid_id)

        wait_for_none_active(test_delay_event, 1800 + id_offerer)
        wait_for_none_active(test_delay_event, 1800 + id_bidder)

        if with_mercy is False:
            # Test manually redeeming the no-script lock tx
            offerer_key = read_json_api(
                1800 + id_offerer,
                "bids/{}".format(bid_id.hex()),
                {"chainbkeysplit": True},
            )["splitkey"]
            data = {"spendchainblocktx": True, "remote_key": offerer_key}
            redeemed_txid = read_json_api(
                1800 + id_bidder, "bids/{}".format(bid_id.hex()), data
            )["txid"]
            assert len(redeemed_txid) == 64

    def do_test_04_follower_recover_b_lock_tx(
        self, coin_from, coin_to, lock_value: int = 32
    ):
        logging.info(
            "---------- Test {} to {} follower recovers coin b lock tx".format(
                coin_from.name, coin_to.name
            )
        )

        id_offerer: int = self.node_a_id
        id_bidder: int = self.node_b_id

        swap_clients = self.swap_clients
        reverse_bid: bool = swap_clients[0].is_reverse_ads_bid(coin_from, coin_to)
        ci_from = swap_clients[id_offerer].ci(coin_from)
        ci_to = swap_clients[id_offerer].ci(coin_to)

        id_offerer: int = id_offerer
        id_bidder: int = id_bidder
        id_leader: int = id_bidder if reverse_bid else id_offerer
        id_follower: int = id_offerer if reverse_bid else id_bidder
        logging.info(
            f"Offerer, bidder, leader, follower: {id_offerer}, {id_bidder}, {id_leader}, {id_follower}"
        )

        js_w0_before = read_json_api(1800 + id_offerer, "wallets")
        js_w1_before = read_json_api(1800 + id_bidder, "wallets")

        amt_swap = ci_from.make_int(random.uniform(0.1, 2.0), r=1)
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)
        logging.info(
            f"amount from, rate, amount to: {amt_swap}, {rate_swap}, {amt_swap * rate_swap}"
        )
        offer_id = swap_clients[id_offerer].postOffer(
            coin_from,
            coin_to,
            amt_swap,
            rate_swap,
            amt_swap,
            SwapTypes.XMR_SWAP,
            lock_type=TxLockTypes.SEQUENCE_LOCK_BLOCKS,
            lock_value=lock_value,
        )
        wait_for_offer(test_delay_event, swap_clients[id_bidder], offer_id)
        offer = swap_clients[id_bidder].getOffer(offer_id)

        bid_id = swap_clients[id_bidder].postXmrBid(offer_id, offer.amount_from)
        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            BidStates.BID_RECEIVED,
            wait_for=(self.extra_wait_time + 40),
        )

        swap_clients[id_follower].setBidDebugInd(
            bid_id, DebugTypes.CREATE_INVALID_COIN_B_LOCK
        )
        swap_clients[id_offerer].acceptBid(bid_id)

        leader_sent_bid: bool = True if reverse_bid else False
        wait_for_bid(
            test_delay_event,
            swap_clients[id_leader],
            bid_id,
            BidStates.XMR_SWAP_FAILED_REFUNDED,
            wait_for=(self.extra_wait_time + 200),
            sent=leader_sent_bid,
        )
        wait_for_bid(
            test_delay_event,
            swap_clients[id_follower],
            bid_id,
            BidStates.XMR_SWAP_FAILED_REFUNDED,
            sent=(not leader_sent_bid),
            wait_for=(self.extra_wait_time + 30),
        )

        js_w0_after = read_json_api(1800 + id_offerer, "wallets")
        js_w1_after = read_json_api(1800 + id_bidder, "wallets")

        node0_from_before = self.getBalance(js_w0_before, coin_from)
        node0_from_after = self.getBalance(js_w0_after, coin_from)
        logging.info(
            "node0 end coin_from balance {}, diff {}".format(
                node0_from_after, node0_from_after - node0_from_before
            )
        )
        node0_to_before = self.getBalance(js_w0_before, coin_to)
        node0_to_after = self.getBalance(js_w0_after, coin_to)
        logging.info(
            "node0 end coin_to balance {}, diff {}".format(
                node0_to_after, node0_to_after - node0_to_before
            )
        )
        max_fee_from: float = 0.1 if coin_from == Coins.PART_ANON else 0.02
        if coin_from != Coins.PART:  # TODO: Discard block rewards
            assert node0_from_before - node0_from_after < max_fee_from

        node1_from_before = self.getBalance(js_w1_before, coin_from)
        node1_from_after = self.getBalance(js_w1_after, coin_from)
        logging.info(
            "node1 end coin_from balance {}, diff {}".format(
                node1_from_after, node1_from_after - node1_from_before
            )
        )
        node1_to_before = self.getBalance(js_w1_before, coin_to)
        node1_to_after = self.getBalance(js_w1_after, coin_to)
        logging.info(
            "node1 end coin_to balance {}, diff {}".format(
                node1_to_after, node1_to_after - node1_to_before
            )
        )

        max_fee_to: float = 0.1 if coin_to == Coins.PART_ANON else 0.02
        assert node1_to_before - node1_to_after < max_fee_to

    def do_test_05_self_bid(self, coin_from, coin_to):
        logging.info(
            "---------- Test {} to {} same client".format(coin_from.name, coin_to.name)
        )

        id_both: int = self.node_b_id

        swap_clients = self.swap_clients
        ci_from = swap_clients[id_both].ci(coin_from)
        ci_to = swap_clients[id_both].ci(coin_to)

        amt_swap = ci_from.make_int(random.uniform(0.1, 2.0), r=1)
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)

        offer_id = swap_clients[id_both].postOffer(
            coin_from,
            coin_to,
            amt_swap,
            rate_swap,
            amt_swap,
            SwapTypes.XMR_SWAP,
            auto_accept_bids=True,
        )
        bid_id = swap_clients[id_both].postXmrBid(offer_id, amt_swap)

        wait_for_bid(
            test_delay_event,
            swap_clients[id_both],
            bid_id,
            BidStates.SWAP_COMPLETED,
            wait_for=(self.extra_wait_time + 180),
        )

    def do_test_08_insufficient_funds(self, coin_from, coin_to):
        logging.info(
            "---------- Test {} to {} Insufficient Funds".format(
                coin_from.name, coin_to.name
            )
        )
        swap_clients = self.swap_clients
        reverse_bid: bool = swap_clients[0].is_reverse_ads_bid(coin_from, coin_to)

        id_offerer: int = self.node_c_id
        id_bidder: int = self.node_b_id

        self.prepare_balance(
            coin_from,
            10.0,
            1800 + id_offerer,
            1801 if coin_from in (Coins.XMR,) else 1800,
        )
        jsw = read_json_api(1800 + id_offerer, "wallets")
        balance_from_before: float = self.getBalance(jsw, coin_from)
        self.prepare_balance(
            coin_to,
            balance_from_before * 3,
            1800 + id_bidder,
            1801 if coin_to in (Coins.XMR,) else 1800,
        )

        swap_clients = self.swap_clients
        ci_from = swap_clients[id_offerer].ci(coin_from)
        ci_to = swap_clients[id_bidder].ci(coin_to)

        amt_swap: int = ci_from.make_int(balance_from_before, r=1)
        rate_swap: int = ci_to.make_int(2.0, r=1)

        try:
            offer_id = swap_clients[id_offerer].postOffer(
                coin_from,
                coin_to,
                amt_swap,
                rate_swap,
                amt_swap,
                SwapTypes.XMR_SWAP,
                auto_accept_bids=True,
            )
        except Exception as e:
            assert "Insufficient funds" in str(e)
        else:
            assert False, "Should fail"

        # Test that postbid errors when offer is for the full balance
        id_offerer_test_bid = id_bidder
        id_bidder_test_bid = id_offerer
        amt_swap_test_bid_to: int = ci_from.make_int(balance_from_before, r=1)
        amt_swap_test_bid_from: int = ci_to.make_int(1.0)
        offer_id = swap_clients[id_offerer_test_bid].postOffer(
            coin_to,
            coin_from,
            amt_swap_test_bid_from,
            0,
            amt_swap_test_bid_from,
            SwapTypes.XMR_SWAP,
            extra_options={"amount_to": amt_swap_test_bid_to},
        )
        wait_for_offer(test_delay_event, swap_clients[id_bidder_test_bid], offer_id)
        try:
            bid_id = swap_clients[id_bidder_test_bid].postBid(
                offer_id, amt_swap_test_bid_from
            )
        except Exception as e:
            assert "Insufficient funds" in str(e)
        else:
            assert False, "Should fail"

        amt_swap -= ci_from.make_int(1)
        rate_swap = ci_to.make_int(1.0, r=1)
        offer_id = swap_clients[id_offerer].postOffer(
            coin_from,
            coin_to,
            amt_swap,
            rate_swap,
            amt_swap,
            SwapTypes.XMR_SWAP,
            auto_accept_bids=True,
        )
        wait_for_offer(test_delay_event, swap_clients[id_bidder], offer_id)

        # First bid should work
        bid_id = swap_clients[id_bidder].postXmrBid(offer_id, amt_swap)
        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            (
                (BidStates.SWAP_COMPLETED, BidStates.XMR_SWAP_NOSCRIPT_COIN_LOCKED)
                if reverse_bid
                else (BidStates.BID_ACCEPTED, BidStates.XMR_SWAP_SCRIPT_COIN_LOCKED)
            ),
            wait_for=120,
        )

        # Should be out of funds for second bid (over remaining offer value causes a hard auto accept fail)
        bid_id = swap_clients[id_bidder].postXmrBid(offer_id, amt_swap)
        wait_for_bid(
            test_delay_event,
            swap_clients[id_offerer],
            bid_id,
            BidStates.BID_AACCEPT_FAIL,
            wait_for=40,
        )
        event = wait_for_event(
            test_delay_event,
            swap_clients[id_offerer],
            Concepts.BID,
            bid_id,
            event_type=EventLogTypes.AUTOMATION_CONSTRAINT,
        )
        assert "Over remaining offer value" in event.event_msg
        try:
            swap_clients[id_offerer].acceptBid(bid_id)
        except Exception as e:
            assert "Insufficient funds" in str(e) or "Balance too low" in str(e)
        else:
            assert False, "Should fail"


class BasicSwapTest(TestFunctions):

    test_fee_rate: int = 1000  # sats/kvB

    @classmethod
    def setUpClass(cls):
        super(BasicSwapTest, cls).setUpClass()
        if False:
            for client in cls.swap_clients:
                client.log.safe_logs = True
                client.log.safe_logs_prefix = b"tests"

    def test_001_nested_segwit(self):
        # p2sh-p2wpkh
        logging.info(
            "---------- Test {} p2sh nested segwit".format(self.test_coin_from.name)
        )
        ci = self.swap_clients[0].ci(self.test_coin_from)

        addr_p2sh_segwit = ci.rpc_wallet(
            "getnewaddress", ["segwit test", "p2sh-segwit"]
        )
        addr_info = ci.rpc_wallet(
            "getaddressinfo",
            [
                addr_p2sh_segwit,
            ],
        )
        assert addr_info["script"] == "witness_v0_keyhash"

        txid = ci.rpc_wallet("sendtoaddress", [addr_p2sh_segwit, 1.0])
        assert len(txid) == 64

        self.mineBlock()
        ro = ci.rpc("scantxoutset", ["start", ["addr({})".format(addr_p2sh_segwit)]])
        assert len(ro["unspents"]) == 1
        assert ro["unspents"][0]["txid"] == txid

        tx_wallet = ci.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )["hex"]
        tx = ci.rpc(
            "decoderawtransaction",
            [
                tx_wallet,
            ],
        )

        prevout_n = -1
        for txo in tx["vout"]:
            if addr_p2sh_segwit == txo["scriptPubKey"]["address"]:
                prevout_n = txo["n"]
                break
        assert prevout_n > -1

        tx_funded = ci.rpc(
            "createrawtransaction",
            [[{"txid": txid, "vout": prevout_n}], {addr_p2sh_segwit: 0.99}],
        )
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded,
            ],
        )["hex"]
        tx_funded_decoded = ci.rpc(
            "decoderawtransaction",
            [
                tx_funded,
            ],
        )
        tx_signed_decoded = ci.rpc(
            "decoderawtransaction",
            [
                tx_signed,
            ],
        )
        assert tx_funded_decoded["txid"] != tx_signed_decoded["txid"]

        # Add scriptsig for txids to match
        addr_p2sh_segwit_info = ci.rpc_wallet(
            "getaddressinfo",
            [
                addr_p2sh_segwit,
            ],
        )
        decoded_tx = from_hex(CTransaction(), tx_funded)
        decoded_tx.vin[0].scriptSig = bytes.fromhex("16" + addr_p2sh_segwit_info["hex"])
        txid_with_scriptsig = decoded_tx.rehash()
        assert txid_with_scriptsig == tx_signed_decoded["txid"]

    def test_002_native_segwit(self):
        # p2wpkh
        logging.info(
            "---------- Test {} p2sh native segwit".format(self.test_coin_from.name)
        )
        ci = self.swap_clients[0].ci(self.test_coin_from)

        addr_segwit = ci.rpc_wallet("getnewaddress", ["segwit test", "bech32"])
        addr_info = ci.rpc_wallet(
            "getaddressinfo",
            [
                addr_segwit,
            ],
        )
        assert addr_info["iswitness"] is True

        txid = ci.rpc_wallet("sendtoaddress", [addr_segwit, 1.0])
        assert len(txid) == 64
        tx_wallet = ci.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )["hex"]
        tx = ci.rpc(
            "decoderawtransaction",
            [
                tx_wallet,
            ],
        )

        self.mineBlock()
        ro = ci.rpc("scantxoutset", ["start", ["addr({})".format(addr_segwit)]])
        assert len(ro["unspents"]) == 1
        assert ro["unspents"][0]["txid"] == txid

        prevout_n = -1
        for txo in tx["vout"]:
            if addr_segwit == txo["scriptPubKey"]["address"]:
                prevout_n = txo["n"]
                break
        assert prevout_n > -1

        tx_funded = ci.rpc(
            "createrawtransaction",
            [[{"txid": txid, "vout": prevout_n}], {addr_segwit: 0.99}],
        )
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded,
            ],
        )["hex"]
        tx_funded_decoded = ci.rpc(
            "decoderawtransaction",
            [
                tx_funded,
            ],
        )
        tx_signed_decoded = ci.rpc(
            "decoderawtransaction",
            [
                tx_signed,
            ],
        )
        assert tx_funded_decoded["txid"] == tx_signed_decoded["txid"]

    def test_003_cltv(self):
        logging.info("---------- Test {} cltv".format(self.test_coin_from.name))
        ci = self.swap_clients[0].ci(self.test_coin_from)

        self.check_softfork_active("bip65")

        chain_height = self.callnoderpc("getblockcount")
        script = CScript(
            [
                chain_height + 3,
                OP_CHECKLOCKTIMEVERIFY,
            ]
        )

        script_dest = ci.getScriptDest(script)
        tx = CTransaction()
        tx.nVersion = ci.txVersion()
        tx.vout.append(ci.txoType()(ci.make_int(1.1), script_dest))
        tx_hex = tx.serialize().hex()
        tx_funded = ci.rpc_wallet("fundrawtransaction", [tx_hex])
        utxo_pos = 0 if tx_funded["changepos"] == 1 else 1
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded["hex"],
            ],
        )["hex"]
        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_signed,
            ],
        )

        addr_out = ci.rpc_wallet("getnewaddress", ["cltv test", "bech32"])
        pkh = ci.decodeSegwitAddress(addr_out)
        script_out = ci.getScriptForPubkeyHash(pkh)

        tx_spend = CTransaction()
        tx_spend.nVersion = ci.txVersion()
        tx_spend.nLockTime = chain_height + 3
        tx_spend.vin.append(CTxIn(COutPoint(int(txid, 16), utxo_pos)))
        tx_spend.vout.append(ci.txoType()(ci.make_int(1.0999), script_out))
        tx_spend.wit.vtxinwit.append(CTxInWitness())
        tx_spend.wit.vtxinwit[0].scriptWitness.stack = [
            script,
        ]
        tx_spend_hex = tx_spend.serialize().hex()

        tx_spend.nLockTime = chain_height + 2
        tx_spend_invalid_hex = tx_spend.serialize().hex()

        for tx_hex in [tx_spend_invalid_hex, tx_spend_hex]:
            try:
                txid = self.callnoderpc(
                    "sendrawtransaction",
                    [
                        tx_hex,
                    ],
                )
            except Exception as e:
                assert "non-final" in str(e)
            else:
                assert False, "Should fail"

        self.mineBlock(5)
        try:
            txid = ci.rpc(
                "sendrawtransaction",
                [
                    tx_spend_invalid_hex,
                ],
            )
        except Exception as e:
            assert "Locktime requirement not satisfied" in str(e)
        else:
            assert False, "Should fail"

        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_spend_hex,
            ],
        )
        self.mineBlock()
        ro = ci.rpc_wallet(
            "listreceivedbyaddress",
            [
                0,
            ],
        )
        sum_addr = 0
        for entry in ro:
            if entry["address"] == addr_out:
                sum_addr += entry["amount"]
        assert sum_addr == 1.0999

        # Ensure tx was mined
        tx_wallet = ci.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )
        assert len(tx_wallet["blockhash"]) == 64

    def test_004_csv(self):
        logging.info("---------- Test {} csv".format(self.test_coin_from.name))
        ci = self.swap_clients[0].ci(self.test_coin_from)

        self.check_softfork_active("csv")

        script = CScript(
            [
                3,
                OP_CHECKSEQUENCEVERIFY,
            ]
        )

        script_dest = ci.getScriptDest(script)
        tx = CTransaction()
        tx.nVersion = ci.txVersion()
        tx.vout.append(ci.txoType()(ci.make_int(1.1), script_dest))
        tx_hex = tx.serialize().hex()
        tx_funded = ci.rpc_wallet("fundrawtransaction", [tx_hex])
        utxo_pos = 0 if tx_funded["changepos"] == 1 else 1
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded["hex"],
            ],
        )["hex"]
        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_signed,
            ],
        )

        addr_out = ci.rpc_wallet("getnewaddress", ["csv test", "bech32"])
        pkh = ci.decodeSegwitAddress(addr_out)
        script_out = ci.getScriptForPubkeyHash(pkh)

        # Double check output type
        prev_tx = ci.rpc(
            "decoderawtransaction",
            [
                tx_signed,
            ],
        )
        assert (
            prev_tx["vout"][utxo_pos]["scriptPubKey"]["type"] == "witness_v0_scripthash"
        )

        tx_spend = CTransaction()
        tx_spend.nVersion = ci.txVersion()
        tx_spend.vin.append(CTxIn(COutPoint(int(txid, 16), utxo_pos), nSequence=3))
        tx_spend.vout.append(ci.txoType()(ci.make_int(1.0999), script_out))
        tx_spend.wit.vtxinwit.append(CTxInWitness())
        tx_spend.wit.vtxinwit[0].scriptWitness.stack = [
            script,
        ]
        tx_spend_hex = tx_spend.serialize().hex()
        try:
            txid = ci.rpc(
                "sendrawtransaction",
                [
                    tx_spend_hex,
                ],
            )
        except Exception as e:
            assert "non-BIP68-final" in str(e)
        else:
            assert False, "Should fail"

        self.mineBlock(3)
        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_spend_hex,
            ],
        )
        self.mineBlock(1)
        ro = ci.rpc_wallet(
            "listreceivedbyaddress",
            [
                0,
            ],
        )
        sum_addr = 0
        for entry in ro:
            if entry["address"] == addr_out:
                sum_addr += entry["amount"]
        assert sum_addr == 1.0999

        # Ensure tx was mined
        tx_wallet = ci.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )
        assert len(tx_wallet["blockhash"]) == 64

    def test_005_watchonly(self):
        logging.info("---------- Test {} watchonly".format(self.test_coin_from.name))
        ci = self.swap_clients[0].ci(self.test_coin_from)
        ci1 = self.swap_clients[1].ci(self.test_coin_from)

        addr = ci.rpc_wallet("getnewaddress", ["watchonly test", "bech32"])
        ro = ci1.rpc_wallet("importaddress", [addr, "", False])
        txid = ci.rpc_wallet("sendtoaddress", [addr, 1.0])
        tx_hex = ci.rpc(
            "getrawtransaction",
            [
                txid,
            ],
        )
        ci1.rpc_wallet(
            "sendrawtransaction",
            [
                tx_hex,
            ],
        )
        ro = ci1.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )
        assert ro["txid"] == txid
        balances = ci1.rpc_wallet("getbalances")
        assert (
            balances["watchonly"]["trusted"]
            + balances["watchonly"]["untrusted_pending"]
            >= 1.0
        )

    def test_006_getblock_verbosity(self):
        logging.info(
            "---------- Test {} getblock verbosity".format(self.test_coin_from.name)
        )

        best_hash = self.callnoderpc("getbestblockhash")
        block = self.callnoderpc("getblock", [best_hash, 2])
        assert "vin" in block["tx"][0]

    def test_007_hdwallet(self):
        logging.info("---------- Test {} hdwallet".format(self.test_coin_from.name))
        ci = self.swap_clients[0].ci(self.test_coin_from)

        if hasattr(ci, "_use_descriptors") and ci._use_descriptors:
            logging.warning("Skipping test")
            return

        test_wif: str = (
            self.swap_clients[0]
            .ci(self.test_coin_from)
            .encodeKey(bytes.fromhex(test_seed))
        )
        new_wallet_name: str = random.randbytes(10).hex()
        # wallet_name, wallet_name, blank, passphrase, avoid_reuse, descriptors
        self.callnoderpc(
            "createwallet", [new_wallet_name, False, True, "", False, False]
        )
        self.callnoderpc("sethdseed", [True, test_wif], wallet=new_wallet_name)

        wi = self.callnoderpc("getwalletinfo", wallet=new_wallet_name)
        assert wi["hdseedid"] == "3da5c0af91879e8ce97d9a843874601c08688078"

        addr = self.callnoderpc("getnewaddress", wallet=new_wallet_name)
        addr_info = self.callnoderpc(
            "getaddressinfo",
            [
                addr,
            ],
            wallet=new_wallet_name,
        )
        assert addr_info["hdmasterfingerprint"] == "a55b7ea9"
        assert addr_info["hdkeypath"] == "m/0'/0'/0'"
        assert addr == "bcrt1qps7hnjd866e9ynxadgseprkc2l56m00dvwargr"

        addr_change = self.callnoderpc("getrawchangeaddress", wallet=new_wallet_name)
        addr_info = self.callnoderpc(
            "getaddressinfo",
            [
                addr_change,
            ],
            wallet=new_wallet_name,
        )
        assert addr_info["hdmasterfingerprint"] == "a55b7ea9"
        assert addr_info["hdkeypath"] == "m/0'/1'/0'"
        assert addr_change == "bcrt1qdl9ryxkqjltv42lhfnqgdjf9tagxsjpp2xak9a"
        self.callnoderpc("unloadwallet", [new_wallet_name])

        self.swap_clients[0].initialiseWallet(Coins.BTC, raise_errors=True)
        assert self.swap_clients[0].checkWalletSeed(Coins.BTC) is True
        for i in range(1500):
            ci.rpc_wallet("getnewaddress")
        assert self.swap_clients[0].checkWalletSeed(Coins.BTC) is True

        rv = read_json_api(1800, "getcoinseed", {"coin": "XMR"})
        assert (
            rv["address"]
            == "47H7UDLzYEsR28BWttxp59SP1UVSxs4VKDJYSfmz7Wd4Fue5VWuoV9x9eejunwzVSmHWN37gBkaAPNf9VD4bTvwQKsBVWyK"
        )

    def test_008_gettxout(self):
        logging.info("---------- Test {} gettxout".format(self.test_coin_from.name))

        swap_client = self.swap_clients[0]
        ci = swap_client.ci(self.test_coin_from)

        addr_1 = ci.getNewAddress(True, "gettxout test 1")
        txid = ci.rpc_wallet("sendtoaddress", [addr_1, 1.0])
        assert len(txid) == 64

        self.mineBlock()

        unspents = ci.rpc_wallet(
            "listunspent",
            [
                0,
                999999999,
                [
                    addr_1,
                ],
            ],
        )
        assert len(unspents) == 1

        utxo = unspents[0]
        txout = ci.rpc("gettxout", [utxo["txid"], utxo["vout"]])
        if "address" in txout["scriptPubKey"]:
            assert addr_1 == txout["scriptPubKey"]["address"]
        else:
            assert addr_1 in txout["scriptPubKey"]["addresses"]
        # Spend
        addr_2 = ci.getNewAddress(True, "gettxout test 2")
        tx_funded = ci.rpc(
            "createrawtransaction",
            [[{"txid": utxo["txid"], "vout": utxo["vout"]}], {addr_2: 0.99}],
        )
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded,
            ],
        )["hex"]
        ci.rpc(
            "sendrawtransaction",
            [
                tx_signed,
            ],
        )

        # utxo should be unavailable when spent in the mempool
        txout = ci.rpc("gettxout", [utxo["txid"], utxo["vout"]])
        assert txout is None

    def test_009_scantxoutset(self):
        logging.info("---------- Test {} scantxoutset".format(self.test_coin_from.name))
        ci = self.swap_clients[0].ci(self.test_coin_from)

        addr_1 = ci.getNewAddress(True, "scantxoutset test")
        txid = ci.rpc_wallet("sendtoaddress", [addr_1, 1.0])
        assert len(txid) == 64

        self.mineBlock()

        ro = ci.rpc("scantxoutset", ["start", ["addr({})".format(addr_1)]])
        assert len(ro["unspents"]) == 1
        assert ro["unspents"][0]["txid"] == txid

    def test_010_txn_size(self):
        logging.info("---------- Test {} txn_size".format(self.test_coin_from.name))

        swap_clients = self.swap_clients
        ci = swap_clients[0].ci(self.test_coin_from)
        pi = swap_clients[0].pi(SwapTypes.XMR_SWAP)

        amount: int = ci.make_int(random.uniform(0.1, 2.0), r=1)

        # Record unspents before createSCLockTx as the used ones will be locked
        unspents = ci.rpc_wallet("listunspent")
        lockedunspents_before = ci.rpc_wallet("listlockunspent")
        a = ci.getNewRandomKey()
        b = ci.getNewRandomKey()

        A = ci.getPubkey(a)
        B = ci.getPubkey(b)
        lock_tx_script = pi.genScriptLockTxScript(ci, A, B)

        lock_tx = ci.createSCLockTx(amount, lock_tx_script)
        lock_tx = ci.fundSCLockTx(lock_tx, self.test_fee_rate)
        lock_tx = ci.signTxWithWallet(lock_tx)

        # Check that inputs were locked
        lockedunspents = ci.rpc_wallet("listlockunspent")
        assert len(lockedunspents) > len(lockedunspents_before)
        unspents_after = ci.rpc_wallet("listunspent")
        for utxo in unspents_after:
            for locked_utxo in lockedunspents:
                if (
                    locked_utxo["txid"] == utxo["txid"]
                    and locked_utxo["vout"] == utxo["vout"]
                ):
                    raise ValueError("Locked utxo in listunspent")

        tx_decoded = ci.rpc("decoderawtransaction", [lock_tx.hex()])
        txid = tx_decoded["txid"]

        vsize = tx_decoded["vsize"]
        expect_fee_int = round(self.test_fee_rate * vsize / 1000)

        out_value: int = 0
        for txo in tx_decoded["vout"]:
            if "value" in txo:
                out_value += ci.make_int(txo["value"])
        in_value: int = 0
        for txi in tx_decoded["vin"]:
            for utxo in unspents:
                if "vout" not in utxo:
                    continue
                if utxo["txid"] == txi["txid"] and utxo["vout"] == txi["vout"]:
                    in_value += ci.make_int(utxo["amount"])
                    break
        fee_value = in_value - out_value

        ci.rpc("sendrawtransaction", [lock_tx.hex()])
        rv = ci.rpc_wallet("gettransaction", [txid])
        wallet_tx_fee = -ci.make_int(rv["fee"])

        assert wallet_tx_fee == fee_value
        assert wallet_tx_fee == expect_fee_int

        addr_out = ci.getNewAddress(True)
        pkh_out = ci.decodeAddress(addr_out)
        fee_info = {}
        lock_spend_tx = ci.createSCLockSpendTx(
            lock_tx, lock_tx_script, pkh_out, self.test_fee_rate, fee_info=fee_info
        )
        vsize_estimated: int = fee_info["vsize"]

        tx_decoded = ci.rpc("decoderawtransaction", [lock_spend_tx.hex()])
        txid = tx_decoded["txid"]

        witness_stack = [
            b"",
            ci.signTx(a, lock_spend_tx, 0, lock_tx_script, amount),
            ci.signTx(b, lock_spend_tx, 0, lock_tx_script, amount),
            lock_tx_script,
        ]
        lock_spend_tx = ci.setTxSignature(lock_spend_tx, witness_stack)
        tx_decoded = ci.rpc("decoderawtransaction", [lock_spend_tx.hex()])
        vsize_actual: int = tx_decoded["vsize"]

        assert vsize_actual <= vsize_estimated and vsize_estimated - vsize_actual < 4
        assert ci.rpc("sendrawtransaction", [lock_spend_tx.hex()]) == txid

        expect_vsize: int = ci.xmr_swap_a_lock_spend_tx_vsize()
        assert expect_vsize >= vsize_actual
        assert expect_vsize - vsize_actual < 10

        # Test chain b (no-script) lock tx size
        v = ci.getNewRandomKey()
        s = ci.getNewRandomKey()
        S = ci.getPubkey(s)
        lock_tx_b_txid = ci.publishBLockTx(v, S, amount, self.test_fee_rate)

        addr_out = ci.getNewAddress(True)
        lock_tx_b_spend_txid = ci.spendBLockTx(
            lock_tx_b_txid, addr_out, v, s, amount, self.test_fee_rate, 0
        )
        lock_tx_b_spend = ci.getTransaction(lock_tx_b_spend_txid)
        if lock_tx_b_spend is None:
            lock_tx_b_spend = ci.getWalletTransaction(lock_tx_b_spend_txid)
        lock_tx_b_spend_decoded = ci.rpc(
            "decoderawtransaction", [lock_tx_b_spend.hex()]
        )

        expect_vsize: int = ci.xmr_swap_b_lock_spend_tx_vsize()
        assert expect_vsize >= lock_tx_b_spend_decoded["vsize"]
        assert expect_vsize - lock_tx_b_spend_decoded["vsize"] < 10

    def test_011_p2sh(self):
        # Not used in bsx for native-segwit coins
        logging.info("---------- Test {} p2sh".format(self.test_coin_from.name))

        ci = self.swap_clients[0].ci(self.test_coin_from)

        script = CScript(
            [
                2,
                2,
                OP_EQUAL,
            ]
        )

        script_dest = ci.get_p2sh_script_pubkey(script)
        tx = CTransaction()
        tx.nVersion = ci.txVersion()
        tx.vout.append(ci.txoType()(ci.make_int(1.1), script_dest))
        tx_hex = tx.serialize().hex()
        tx_funded = ci.rpc_wallet("fundrawtransaction", [tx_hex])
        utxo_pos = 0 if tx_funded["changepos"] == 1 else 1
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded["hex"],
            ],
        )["hex"]
        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_signed,
            ],
        )

        addr_out = ci.rpc_wallet("getnewaddress", ["csv test", "bech32"])
        pkh = ci.decodeSegwitAddress(addr_out)
        script_out = ci.getScriptForPubkeyHash(pkh)

        # Double check output type
        prev_tx = ci.rpc(
            "decoderawtransaction",
            [
                tx_signed,
            ],
        )
        assert prev_tx["vout"][utxo_pos]["scriptPubKey"]["type"] == "scripthash"

        tx_spend = CTransaction()
        tx_spend.nVersion = ci.txVersion()
        tx_spend.vin.append(
            CTxIn(
                COutPoint(int(txid, 16), utxo_pos),
                scriptSig=CScript(
                    [
                        script,
                    ]
                ),
            )
        )
        tx_spend.vout.append(ci.txoType()(ci.make_int(1.0999), script_out))
        tx_spend_hex = tx_spend.serialize().hex()

        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_spend_hex,
            ],
        )
        self.mineBlock(1)
        ro = ci.rpc_wallet(
            "listreceivedbyaddress",
            [
                0,
            ],
        )
        sum_addr = 0
        for entry in ro:
            if entry["address"] == addr_out:
                sum_addr += entry["amount"]
        assert sum_addr == 1.0999

        # Ensure tx was mined
        tx_wallet = ci.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )
        assert len(tx_wallet["blockhash"]) == 64

    def test_012_p2sh_p2wsh(self):
        # Not used in bsx for native-segwit coins
        logging.info("---------- Test {} p2sh-p2wsh".format(self.test_coin_from.name))

        ci = self.swap_clients[0].ci(self.test_coin_from)

        script = CScript(
            [
                2,
                2,
                OP_EQUAL,
            ]
        )

        script_dest = ci.getP2SHP2WSHDest(script)
        tx = CTransaction()
        tx.nVersion = ci.txVersion()
        tx.vout.append(ci.txoType()(ci.make_int(1.1), script_dest))
        tx_hex = tx.serialize().hex()
        tx_funded = ci.rpc_wallet("fundrawtransaction", [tx_hex])
        utxo_pos = 0 if tx_funded["changepos"] == 1 else 1
        tx_signed = ci.rpc_wallet(
            "signrawtransactionwithwallet",
            [
                tx_funded["hex"],
            ],
        )["hex"]
        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_signed,
            ],
        )

        addr_out = ci.rpc_wallet("getnewaddress", ["csv test", "bech32"])
        pkh = ci.decodeSegwitAddress(addr_out)
        script_out = ci.getScriptForPubkeyHash(pkh)

        # Double check output type
        prev_tx = ci.rpc(
            "decoderawtransaction",
            [
                tx_signed,
            ],
        )
        assert prev_tx["vout"][utxo_pos]["scriptPubKey"]["type"] == "scripthash"

        tx_spend = CTransaction()
        tx_spend.nVersion = ci.txVersion()
        tx_spend.vin.append(
            CTxIn(
                COutPoint(int(txid, 16), utxo_pos),
                scriptSig=ci.getP2SHP2WSHScriptSig(script),
            )
        )
        tx_spend.vout.append(ci.txoType()(ci.make_int(1.0999), script_out))
        tx_spend.wit.vtxinwit.append(CTxInWitness())
        tx_spend.wit.vtxinwit[0].scriptWitness.stack = [
            script,
        ]
        tx_spend_hex = tx_spend.serialize().hex()

        txid = ci.rpc(
            "sendrawtransaction",
            [
                tx_spend_hex,
            ],
        )
        self.mineBlock(1)
        ro = ci.rpc_wallet(
            "listreceivedbyaddress",
            [
                0,
            ],
        )
        sum_addr = 0
        for entry in ro:
            if entry["address"] == addr_out:
                sum_addr += entry["amount"]
        assert sum_addr == 1.0999

        # Ensure tx was mined
        tx_wallet = ci.rpc_wallet(
            "gettransaction",
            [
                txid,
            ],
        )
        assert len(tx_wallet["blockhash"]) == 64

    def test_013_descriptor_wallet(self):
        logging.info(f"---------- Test {self.test_coin_from.name} descriptor wallet")

        ci = self.swap_clients[0].ci(self.test_coin_from)

        ek = ExtKeyPair()
        ek.set_seed(bytes.fromhex(test_seed))
        ek_encoded: str = ci.encode_secret_extkey(ek.encode_v())
        new_wallet_name = "descriptors_" + random.randbytes(10).hex()
        new_watch_wallet_name = "watch_descriptors_" + random.randbytes(10).hex()
        # wallet_name, disable_private_keys, blank, passphrase, avoid_reuse, descriptors
        ci.rpc("createwallet", [new_wallet_name, False, True, "", False, True])
        ci.rpc("createwallet", [new_watch_wallet_name, True, True, "", False, True])

        desc_external = descsum_create(f"wpkh({ek_encoded}/0h/0h/*h)")
        desc_internal = descsum_create(f"wpkh({ek_encoded}/0h/1h/*h)")
        self.callnoderpc(
            "importdescriptors",
            [
                [
                    {
                        "desc": desc_external,
                        "timestamp": "now",
                        "active": True,
                        "range": [0, 10],
                        "next_index": 0,
                    },
                    {
                        "desc": desc_internal,
                        "timestamp": "now",
                        "active": True,
                        "internal": True,
                    },
                ],
            ],
            wallet=new_wallet_name,
        )

        addr = self.callnoderpc(
            "getnewaddress", ["test descriptors"], wallet=new_wallet_name
        )
        addr_info = self.callnoderpc(
            "getaddressinfo",
            [
                addr,
            ],
            wallet=new_wallet_name,
        )
        assert addr_info["hdmasterfingerprint"] == "a55b7ea9"
        assert addr_info["hdkeypath"] == "m/0h/0h/0h"
        if self.test_coin_from == Coins.BTC:
            assert addr == "bcrt1qps7hnjd866e9ynxadgseprkc2l56m00dvwargr"

        addr_change = self.callnoderpc("getrawchangeaddress", wallet=new_wallet_name)
        addr_info = self.callnoderpc(
            "getaddressinfo",
            [
                addr_change,
            ],
            wallet=new_wallet_name,
        )
        assert addr_info["hdmasterfingerprint"] == "a55b7ea9"
        assert addr_info["hdkeypath"] == "m/0h/1h/0h"
        if self.test_coin_from == Coins.BTC:
            assert addr_change == "bcrt1qdl9ryxkqjltv42lhfnqgdjf9tagxsjpp2xak9a"

        desc_watch = descsum_create(f"addr({addr})")
        self.callnoderpc(
            "importdescriptors",
            [
                [
                    {"desc": desc_watch, "timestamp": "now", "active": False},
                ],
            ],
            wallet=new_watch_wallet_name,
        )
        ci.rpc_wallet("sendtoaddress", [addr, 1])
        found: bool = False
        for i in range(10):
            txn_list = self.callnoderpc(
                "listtransactions", ["*", 100, 0, True], wallet=new_watch_wallet_name
            )
            test_delay_event.wait(1)
            if len(txn_list) > 0:
                found = True
                break
        assert found

        # Test that addresses can be generated beyond range in listdescriptors
        for i in range(2000):
            self.callnoderpc(
                "getnewaddress",
                [
                    f"t{i}",
                ],
                wallet=new_wallet_name,
            )

        # https://github.com/bitcoin/bitcoin/issues/10542
        # https://github.com/bitcoin/bitcoin/issues/26046
        sign_for_address: str = self.callnoderpc(
            "getnewaddress",
            [
                "sign address",
            ],
            wallet=new_wallet_name,
        )
        priv_keys = self.callnoderpc("listdescriptors", [True], wallet=new_wallet_name)
        addr_info = self.callnoderpc(
            "getaddressinfo", [sign_for_address], wallet=new_wallet_name
        )
        hdkeypath = addr_info["hdkeypath"]

        sign_for_address_key = None
        for descriptor in priv_keys["descriptors"]:
            if descriptor["active"] is False or descriptor["internal"] is True:
                continue
            desc = descriptor["desc"]
            assert desc.startswith("wpkh(")
            ext_key = desc[5:].split(")")[0].split("/", 1)[0]
            ext_key_data = decodeAddress(ext_key)[4:]
            ci_part = self.swap_clients[0].ci(Coins.PART)
            ext_key_data_part = ci_part.encode_secret_extkey(ext_key_data)
            rv = ci_part.rpc_wallet("extkey", ["info", ext_key_data_part, hdkeypath])
            extkey_derived = rv["key_info"]["result"]
            ext_key_data = decodeAddress(extkey_derived)[4:]
            ek = ExtKeyPair()
            ek.decode(ext_key_data)
            addr = ci.encodeSegwitAddress(ci.getAddressHashFromKey(ek._key))
            assert addr == sign_for_address
            sign_for_address_key = ci.encodeKey(ek._key)
            break
        assert sign_for_address_key is not None
        sign_message: str = "Would be better if dumpprivkey or signmessage worked"
        sig = self.callnoderpc(
            "signmessagewithprivkey",
            [sign_for_address_key, sign_message],
            wallet=new_wallet_name,
        )
        assert ci.verifyMessage(sign_for_address, sign_message, sig)

        self.callnoderpc("unloadwallet", [new_wallet_name])
        self.callnoderpc("unloadwallet", [new_watch_wallet_name])

    def test_014_encrypt_existing_wallet(self):
        logging.info(
            f"---------- Test {self.test_coin_from.name} encrypt existing wallet"
        )

        ci = self.swap_clients[0].ci(self.test_coin_from)
        wallet_name = "encrypt_existing_wallet"

        ci.createWallet(wallet_name)
        ci.setActiveWallet(wallet_name)
        chain_client_settings = self.swap_clients[0].getChainClientSettings(
            self.test_coin_from
        )
        try:
            chain_client_settings["manage_daemon"] = True
            ci.initialiseWallet(ci.getNewRandomKey())

            original_seed_id = ci.getWalletSeedID()

            addr1 = ci.getNewAddress(True)
            addr1_info = ci.rpc_wallet(
                "getaddressinfo",
                [
                    addr1,
                ],
            )

            addr_int1 = ci.rpc_wallet("getrawchangeaddress")
            addr_int1_info = ci.rpc_wallet(
                "getaddressinfo",
                [
                    addr_int1,
                ],
            )

            ci.encryptWallet("test.123")

            after_seed_id = ci.getWalletSeedID()

            addr2 = ci.getNewAddress(True)
            addr2_info = ci.rpc_wallet(
                "getaddressinfo",
                [
                    addr2,
                ],
            )

            addr_int2 = ci.rpc_wallet("getrawchangeaddress")
            addr_int2_info = ci.rpc_wallet(
                "getaddressinfo",
                [
                    addr_int2,
                ],
            )

            key_id_field: str = (
                "hdmasterkeyid"
                if "hdmasterkeyid" in addr1_info
                else "hdmasterfingerprint"
            )
            assert addr1_info[key_id_field] == addr2_info[key_id_field]
            assert addr_int1_info[key_id_field] == addr_int2_info[key_id_field]
            assert addr1_info[key_id_field] == addr_int1_info[key_id_field]
            assert original_seed_id == after_seed_id
        finally:
            ci.setActiveWallet("wallet.dat")
            chain_client_settings["manage_daemon"] = False

    def test_01_0_lock_bad_prevouts(self):
        logging.info(
            "---------- Test {} lock_bad_prevouts".format(self.test_coin_from.name)
        )
        # Lock non segwit prevouts created in earlier tests
        for i in range(2):
            ci = self.swap_clients[i].ci(self.test_coin_from)
            if hasattr(ci, "lockNonSegwitPrevouts"):
                ci.lockNonSegwitPrevouts()

    def test_01_a_full_swap(self):
        if not self.has_segwit:
            return
        self.do_test_01_full_swap(self.test_coin_from, Coins.XMR)

    def test_01_b_full_swap_reverse(self):
        if not self.has_segwit:
            return
        self.prepare_balance(Coins.XMR, 100.0, 1800, 1801)
        self.do_test_01_full_swap(Coins.XMR, self.test_coin_from)

    def test_01_c_full_swap_to_part(self):
        if not self.has_segwit:
            return
        self.do_test_01_full_swap(self.test_coin_from, Coins.PART)

    def test_01_d_full_swap_from_part(self):
        self.do_test_01_full_swap(Coins.PART, self.test_coin_from)

    def test_02_a_leader_recover_a_lock_tx(self):
        if not self.has_segwit:
            return
        self.do_test_02_leader_recover_a_lock_tx(self.test_coin_from, Coins.XMR)

    def test_02_b_leader_recover_a_lock_tx_reverse(self):
        if not self.has_segwit:
            return
        self.prepare_balance(Coins.XMR, 100.0, 1800, 1801)
        self.do_test_02_leader_recover_a_lock_tx(Coins.XMR, self.test_coin_from)

    def test_02_c_leader_recover_a_lock_tx_to_part(self):
        if not self.has_segwit:
            return
        self.do_test_02_leader_recover_a_lock_tx(self.test_coin_from, Coins.PART)

    def test_02_leader_recover_a_lock_tx_from_part(self):
        self.prepare_balance(self.test_coin_from, 100.0, 1801, 1800)
        self.do_test_02_leader_recover_a_lock_tx(Coins.PART, self.test_coin_from)

    def test_03_a_follower_recover_a_lock_tx(self):
        if not self.has_segwit:
            return
        self.do_test_03_follower_recover_a_lock_tx(self.test_coin_from, Coins.XMR)

    def test_03_b_follower_recover_a_lock_tx_reverse(self):
        if not self.has_segwit:
            return
        self.prepare_balance(Coins.XMR, 100.0, 1800, 1801)
        self.do_test_03_follower_recover_a_lock_tx(Coins.XMR, self.test_coin_from)

    def test_03_c_follower_recover_a_lock_tx_to_part(self):
        if not self.has_segwit:
            return
        self.do_test_03_follower_recover_a_lock_tx(self.test_coin_from, Coins.PART)

    def test_03_d_follower_recover_a_lock_tx_from_part(self):
        self.do_test_03_follower_recover_a_lock_tx(Coins.PART, self.test_coin_from)

    def test_03_e_follower_recover_a_lock_tx_mercy_release(self):
        if not self.has_segwit:
            return
        self.do_test_03_follower_recover_a_lock_tx(
            self.test_coin_from, Coins.XMR, with_mercy=True
        )

    def test_03_f_follower_recover_a_lock_tx_mercy_release_reverse(self):
        if not self.has_segwit:
            return
        self.prepare_balance(Coins.XMR, 100.0, 1800, 1801)
        self.prepare_balance(self.test_coin_from, 100.0, 1801, 1800)
        self.do_test_03_follower_recover_a_lock_tx(
            Coins.XMR, self.test_coin_from, with_mercy=True
        )

    def test_04_a_follower_recover_b_lock_tx(self):
        if not self.has_segwit:
            return
        self.do_test_04_follower_recover_b_lock_tx(self.test_coin_from, Coins.XMR)

    def test_04_b_follower_recover_b_lock_tx_reverse(self):
        if not self.has_segwit:
            return
        self.prepare_balance(Coins.XMR, 100.0, 1800, 1801)
        self.do_test_04_follower_recover_b_lock_tx(Coins.XMR, self.test_coin_from)

    def test_04_c_follower_recover_b_lock_tx_to_part(self):
        if not self.has_segwit:
            return
        self.do_test_04_follower_recover_b_lock_tx(self.test_coin_from, Coins.PART)

    def test_04_d_follower_recover_b_lock_tx_from_part(self):
        self.do_test_04_follower_recover_b_lock_tx(Coins.PART, self.test_coin_from)

    def test_05_self_bid(self):
        if not self.has_segwit:
            return
        self.do_test_05_self_bid(self.test_coin_from, Coins.XMR)

    def test_05_self_bid_to_part(self):
        if not self.has_segwit:
            return
        self.do_test_05_self_bid(self.test_coin_from, Coins.PART)

    def test_05_self_bid_from_part(self):
        if not self.has_segwit:
            return
        self.do_test_05_self_bid(Coins.PART, self.test_coin_from)

    def test_05_self_bid_rev(self):
        if not self.has_segwit:
            return
        self.do_test_05_self_bid(Coins.XMR, self.test_coin_from)

    def test_06_preselect_inputs(self):
        tla_from = self.test_coin_from.name
        logging.info("---------- Test {} Preselected inputs".format(tla_from))
        swap_clients = self.swap_clients

        self.prepare_balance(self.test_coin_from, 100.0, 1802, 1800)

        js_w2 = read_json_api(1802, "wallets")
        assert float(js_w2[tla_from]["balance"]) >= 100.0

        js_w2 = read_json_api(1802, "wallets")
        post_json = {
            "value": float(js_w2[tla_from]["balance"]),
            "address": read_json_api(
                1802, "wallets/{}/nextdepositaddr".format(tla_from.lower())
            ),
            "subfee": True,
        }
        json_rv = read_json_api(
            1802, "wallets/{}/withdraw".format(tla_from.lower()), post_json
        )
        wait_for_balance(
            test_delay_event,
            "http://127.0.0.1:1802/json/wallets/{}".format(tla_from.lower()),
            "balance",
            10.0,
        )
        assert len(json_rv["txid"]) == 64

        # Create prefunded ITX
        ci = swap_clients[2].ci(self.test_coin_from)
        ci_to = swap_clients[2].ci(Coins.XMR)
        pi = swap_clients[2].pi(SwapTypes.XMR_SWAP)
        js_w2 = read_json_api(1802, "wallets")
        swap_value = ci.make_int(js_w2[tla_from]["balance"])
        assert swap_value > ci.make_int(95)

        itx = pi.getFundedInitiateTxTemplate(ci, swap_value, True)
        itx_decoded = ci.describeTx(itx.hex())
        n = pi.findMockVout(ci, itx_decoded)
        value_after_subfee = ci.make_int(itx_decoded["vout"][n]["value"])
        assert value_after_subfee < swap_value
        swap_value = value_after_subfee
        wait_for_unspent(test_delay_event, ci, swap_value)

        extra_options = {"prefunded_itx": itx}
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)
        offer_id = swap_clients[2].postOffer(
            self.test_coin_from,
            Coins.XMR,
            swap_value,
            rate_swap,
            swap_value,
            SwapTypes.XMR_SWAP,
            extra_options=extra_options,
        )

        wait_for_offer(test_delay_event, swap_clients[1], offer_id)
        offer = swap_clients[1].getOffer(offer_id)
        bid_id = swap_clients[1].postBid(offer_id, offer.amount_from)

        wait_for_bid(
            test_delay_event,
            swap_clients[2],
            bid_id,
            BidStates.BID_RECEIVED,
            wait_for=(self.extra_wait_time + 40),
        )
        swap_clients[2].acceptBid(bid_id)

        wait_for_bid(
            test_delay_event,
            swap_clients[2],
            bid_id,
            BidStates.SWAP_COMPLETED,
            wait_for=120,
        )
        wait_for_bid(
            test_delay_event,
            swap_clients[1],
            bid_id,
            BidStates.SWAP_COMPLETED,
            sent=True,
            wait_for=120,
        )

        # Verify expected inputs were used
        bid, _, _, _, _ = swap_clients[2].getXmrBidAndOffer(bid_id)
        assert bid.xmr_a_lock_tx
        wtx = ci.rpc_wallet(
            "gettransaction",
            [
                bid.xmr_a_lock_tx.txid.hex(),
            ],
        )
        itx_after = ci.describeTx(wtx["hex"])
        assert len(itx_after["vin"]) == len(itx_decoded["vin"])
        for i, txin in enumerate(itx_decoded["vin"]):
            txin_after = itx_after["vin"][i]
            assert txin["txid"] == txin_after["txid"]
            assert txin["vout"] == txin_after["vout"]

    def test_07_expire_stuck_accepted(self):
        coin_from, coin_to = (self.test_coin_from, Coins.XMR)
        logging.info(
            "---------- Test {} to {} expires bid stuck on accepted".format(
                coin_from.name, coin_to.name
            )
        )

        swap_clients = self.swap_clients
        ci_to = swap_clients[0].ci(coin_to)

        amt_swap = make_int(random.uniform(0.1, 2.0), scale=8, r=1)
        rate_swap = ci_to.make_int(random.uniform(0.2, 20.0), r=1)

        offer_id = swap_clients[0].postOffer(
            coin_from,
            coin_to,
            amt_swap,
            rate_swap,
            amt_swap,
            SwapTypes.XMR_SWAP,
            auto_accept_bids=True,
        )
        wait_for_offer(test_delay_event, swap_clients[1], offer_id)
        bid_id = swap_clients[1].postXmrBid(offer_id, amt_swap)
        swap_clients[1].abandonBid(bid_id)
        wait_for_bid(
            test_delay_event,
            swap_clients[0],
            bid_id,
            BidStates.BID_ACCEPTED,
            wait_for=(self.extra_wait_time + 40),
        )

        try:
            swap_clients[0].setMockTimeOffset(7200)
            old_check_expired_seconds = swap_clients[0].check_expired_seconds
            swap_clients[0].check_expired_seconds = 1

            wait_for_bid(
                test_delay_event,
                swap_clients[0],
                bid_id,
                BidStates.SWAP_TIMEDOUT,
                wait_for=180,
            )
        finally:
            swap_clients[0].check_expired_seconds = old_check_expired_seconds
            swap_clients[0].setMockTimeOffset(0)

    def test_08_insufficient_funds(self):
        self.do_test_08_insufficient_funds(self.test_coin_from, Coins.XMR)

    def test_08_insufficient_funds_rev(self):
        self.do_test_08_insufficient_funds(Coins.XMR, self.test_coin_from)


class TestBTC(BasicSwapTest):
    __test__ = True
    test_coin_from = Coins.BTC
    start_ltc_nodes = False
    base_rpc_port = BTC_BASE_RPC_PORT

    def test_003_api(self):
        logging.info("---------- Test API")

        help_output = read_json_api(1800, "help")
        assert "getcoinseed" in help_output["commands"]

        rv = read_json_api(1800, "getcoinseed")
        assert rv["error"] == "No post data"

        rv = read_json_api(1800, "getcoinseed", {"coin": "PART"})
        assert "seed is set from the Basicswap mnemonic" in rv["error"]

        rv = read_json_api(1800, "getcoinseed", {"coin": "BTC"})
        assert rv["seed"] == test_seed
        assert rv["seed_id"] in (
            "3da5c0af91879e8ce97d9a843874601c08688078",
            "4a231080ec6f4078e543d39cc6dcf0b922c9b16b",
        )
        assert rv["seed_id"] == rv["expected_seed_id"]

        rv = read_json_api(
            1800,
            "identities/ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F",
            {"set_label": "test 1"},
        )
        assert isinstance(rv, dict)
        assert rv["address"] == "ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F"
        assert rv["label"] == "test 1"
        rv = read_json_api(
            1800,
            "identities/ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F",
            {"set_label": "test 2"},
        )
        assert isinstance(rv, dict)
        assert rv["address"] == "ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F"
        assert rv["label"] == "test 2"

        rv = read_json_api(
            1800,
            "identities/pPCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F",
            {"set_label": "test 3"},
        )
        assert rv["error"] == "Invalid identity address"

        rv = read_json_api(
            1800,
            "identities/ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F",
            {"set_note": "note 1"},
        )
        assert isinstance(rv, dict)
        assert rv["address"] == "ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F"
        assert rv["label"] == "test 2"
        assert rv["note"] == "note 1"

        rv = read_json_api(
            1800,
            "identities/ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F",
            {"set_automation_override": 1},
        )
        assert isinstance(rv, dict)
        assert rv["automation_override"] == 1

        rv = read_json_api(
            1800,
            "identities/ppCsRro5po7Yu6kyu5XjSyr3A1PPdk9j1F",
            {"set_visibility_override": "hide"},
        )
        assert isinstance(rv, dict)
        assert rv["visibility_override"] == 1

        rv = read_json_api(1800, "automationstrategies")
        assert len(rv) == 2

        rv = read_json_api(1800, "automationstrategies/1")
        assert rv["label"] == "Accept All"

        sx_addr = read_json_api(1800, "wallets/part/newstealthaddress")
        assert (
            callnoderpc(
                0,
                "getaddressinfo",
                [
                    sx_addr,
                ],
            )["isstealthaddress"]
            is True
        )

        rv = read_json_api(1800, "wallets/part")
        assert "locked_utxos" in rv

        rv = read_json_api(
            1800, "validateamount", {"coin": "part", "amount": 0.000000015}
        )
        assert "Mantissa too long" in rv["error"]

        rv = read_json_api(
            1800,
            "validateamount",
            {"coin": "part", "amount": 0.000000015, "method": "roundoff"},
        )
        assert rv == "0.00000002"

        rv = read_json_api(
            1800,
            "validateamount",
            {"coin": "part", "amount": 0.000000015, "method": "rounddown"},
        )
        assert rv == "0.00000001"

    def test_009_wallet_encryption(self):

        for coin in ("btc", "part", "xmr"):
            jsw = read_json_api(1800, f"wallets/{coin}")
            assert jsw["encrypted"] is False
            assert jsw["locked"] is False

        read_json_api(
            1800, "setpassword", {"oldpassword": "", "newpassword": "notapassword123"}
        )

        # Entire system is locked with Particl wallet
        jsw = read_json_api(1800, "wallets/btc")
        assert "Coin must be unlocked" in jsw["error"]

        read_json_api(1800, "unlock", {"coin": "part", "password": "notapassword123"})

        for coin in ("btc", "xmr"):
            jsw = read_json_api(1800, f"wallets/{coin}")
            assert jsw["encrypted"] is True
            assert jsw["locked"] is True

        read_json_api(1800, "lock", {"coin": "part"})
        jsw = read_json_api(1800, "wallets/part")
        assert "Coin must be unlocked" in jsw["error"]

        read_json_api(
            1800,
            "setpassword",
            {"oldpassword": "notapassword123", "newpassword": "notapassword456"},
        )
        read_json_api(1800, "unlock", {"password": "notapassword456"})

        for coin in ("part", "btc", "xmr"):
            jsw = read_json_api(1800, f"wallets/{coin}")
            assert jsw["encrypted"] is True
            assert jsw["locked"] is False

    def test_01_full_swap(self):
        abandon_all_swaps(test_delay_event, self.swap_clients[0])
        wait_for_none_active(test_delay_event, 1800)
        js_0 = read_json_api(1800, "wallets")
        if not js_0["PART"]["encrypted"]:
            read_json_api(
                1800,
                "setpassword",
                {"oldpassword": "", "newpassword": "notapassword123"},
            )
            read_json_api(1800, "unlock", {"password": "notapassword123"})
        js_0 = read_json_api(1800, "wallets")
        assert js_0["PART"]["encrypted"] is True
        assert js_0["PART"]["locked"] is False

        super().test_01_a_full_swap()


class TestBTC_PARTB(TestFunctions):
    __test__ = True
    test_coin_from = Coins.BTC
    test_coin_to = Coins.PART_BLIND
    start_ltc_nodes = False
    base_rpc_port = BTC_BASE_RPC_PORT

    @classmethod
    def setUpClass(cls):
        super(TestBTC_PARTB, cls).setUpClass()
        if False:
            for client in cls.swap_clients:
                client.log.safe_logs = True
                client.log.safe_logs_prefix = b"tests"

    def test_01_a_full_swap(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1801, 1800)
        self.do_test_01_full_swap(self.test_coin_from, self.test_coin_to)

    def test_01_b_full_swap_reverse(self):
        self.extra_wait_time = 60
        try:
            self.prepare_balance(self.test_coin_to, 100.0, 1800, 1800)
            self.do_test_01_full_swap(self.test_coin_to, self.test_coin_from)
        finally:
            self.extra_wait_time = 0

    def test_02_a_leader_recover_a_lock_tx(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1801, 1800)
        self.do_test_02_leader_recover_a_lock_tx(self.test_coin_from, self.test_coin_to)

    def test_02_b_leader_recover_a_lock_tx_reverse(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1800, 1800)
        self.do_test_02_leader_recover_a_lock_tx(self.test_coin_to, self.test_coin_from)

    def test_03_a_follower_recover_a_lock_tx(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1801, 1800)
        self.do_test_03_follower_recover_a_lock_tx(
            self.test_coin_from, self.test_coin_to
        )

    def test_03_b_follower_recover_a_lock_tx_reverse(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1800, 1800)
        self.do_test_03_follower_recover_a_lock_tx(
            self.test_coin_to, self.test_coin_from, lock_value=12
        )

    def test_04_a_follower_recover_b_lock_tx(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1801, 1800)
        self.do_test_04_follower_recover_b_lock_tx(
            self.test_coin_from, self.test_coin_to
        )

    def test_04_b_follower_recover_b_lock_tx_reverse(self):
        self.prepare_balance(self.test_coin_to, 100.0, 1800, 1800)
        self.do_test_04_follower_recover_b_lock_tx(
            self.test_coin_to, self.test_coin_from
        )


class TestBTC_PARTA(TestBTC_PARTB):
    __test__ = True
    test_coin_to = Coins.PART_ANON


if __name__ == "__main__":
    unittest.main()
