import click
import config
import uuid
import time
import random
import pandas as pd
from datetime import datetime
from sslsapp import app, db
from sslsapp.models.model import Options, TradeSettings, DciEarnings, Indexes, Loss, Orders, Cookie
import exchange.angel as angel
import alert.discord as discord
import helper.date_ist as date_ist
from strategy.ssl import check_ssl_short
from sqlalchemy import and_, asc, desc, func
from sqlalchemy.orm.exc import NoResultFound


@click.command(name='check_entry')
def check_entry():
    current_datetime = date_ist.ist_time()
    print(current_datetime)

    start_time = datetime.strptime("09:15", "%H:%M").time()
    end_time = datetime.strptime("23:28", "%H:%M").time()

    if start_time <= current_datetime.time() <= end_time:
        process_trade_if_possible()


def process_trade_if_possible():
    index = Indexes.query.filter_by(enabled=True).first()
    in_trade_option = get_in_trade_option(index.name)

    if in_trade_option or is_trade_done_for_today(index.name):
        return

    print(f'Fetching options of index: {index.name}')
    angel_obj = angel.get_angel_obj()

    contract_type = "CE" if config.MARKET_DIRECTION == "DOWN" else "PE"
    process_option_trade(angel_obj, index, contract_type)


def get_in_trade_option(index_name):
    return Options.query.filter_by(in_trade=True, name=index_name).filter(
        Options.instrument_type.in_(['PE', 'CE'])).first()


def is_trade_done_for_today(index_name):
    current_date = datetime.utcnow().date()
    dci_earnings = DciEarnings.query.filter_by(status='ACHIEVED', achieved_date=current_date).first()
    if dci_earnings:
        print(f'Trade - Done for the day: {index_name}')
        return True
    return False


def process_option_trade(angel_obj, index, option_type):
    atm_strike = get_atm_strike(angel_obj, index)
    selected_strikes = get_selected_strikes(atm_strike, index.option_sizing, option_type)

    if not get_in_trade_option(index.name):
        selected_options = Options.query.filter(
            Options.instrument_type == option_type,
            Options.strike.in_(selected_strikes),
            Options.name == index.name
        ).all()

        for option in selected_options:
            df_option = angel.get_3min_olhcv(angel_obj, option)

            # small_candle_index = angel.get_small_candle_index(df_option)
            # print(small_candle_index)
            #
            # if small_candle_index is None:
            #     print("All candles are big")
            #     return

            if check_ssl_short(df_option):
                print('Entering Trade')
                execute_trade(angel_obj, option)
                break


def execute_trade(angel_obj, option):
    # otm_options = get_otm_options(option_type, atm_strike)
    # otm_instrument_tokens = [otm_option.instrument_token for otm_option in otm_options]
    #
    # if otm_instrument_tokens:
    #     closest_hedge, hedge_option = get_closest_hedge_option(angel_obj, otm_instrument_tokens)
    #
    #     if not hedge_option:
    #         return

    margin_required = get_margin_required(angel_obj, option)
    print(f"Margin required: {margin_required}")
    max_lots = calculate_max_lots(margin_required)
    print(f"Lots calculated: {max_lots}")

    if max_lots < 1:
        print("No fund available for trade")
        return

    place_orders(angel_obj, option, max_lots)


def place_orders(angel_obj, option, max_lots):
    trade_setting = TradeSettings.query.first()
    order_link_id = str(uuid.uuid4())
    option.in_trade = True
    option.order_link_id = order_link_id

    if trade_setting.demo:
        return

    # hedge_order_detail = execute_buy_order(angel_obj, hedge_option, max_lots)
    # if not hedge_order_detail:
    #     return False

    [sell_order_detail, main_order] = execute_sell_order(angel_obj, option, max_lots)
    if not sell_order_detail:
        return False

    tp_price = execute_tp_order(angel_obj, max_lots, option, sell_order_detail, trade_setting)

    main_order.min_guarantee_price = main_order.price + (tp_price - main_order.price) * config.GUARANTEE_PERCENTAGE
    db.session.commit()


def execute_tp_order(angel_obj, lots, sell_option, sell_order_detail, trade_setting):
    not_achieved_earning = DciEarnings.query.filter_by(status='NOT-ACHIEVED').order_by(
        DciEarnings.day).first()
    tp = not_achieved_earning.earnings
    loss = db.session.query(func.sum(Loss.loss)).scalar() or 0.0
    tp = tp - loss
    tp_price = calculate_tp_price(lots, sell_order_detail['averageprice'],
                                  tp=tp, lot_size=sell_option.lot_size)
    tp_order_id = create_tp_order(angel_obj, sell_option, tp_price, lots, 'BUY', trade_setting.demo)
    return tp_price


def place_tp_option_order(angel_obj, symbol, token, order_type, side, quantity, price, exchange='NFO'):
    return angel.place_tp_option_order(angel_obj, order_type, symbol, token, side, quantity, price, exchange)


def create_tp_order(angel_obj, in_trade_option, price, lot, side, is_demo):
    if is_demo:
        tp_order_id = generate_random_digit_number(7)
        order_detail = {'price': price}
    else:
        tp_order_id = place_tp_option_order(angel_obj, in_trade_option.symbol, in_trade_option.instrument_token,
                                            'LIMIT',
                                            side, lot * in_trade_option.lot_size, price,
                                            exchange=in_trade_option.exchange)

        if tp_order_id is None:
            alert_msg = f"TP order creation failed for '{in_trade_option.symbol}({in_trade_option.instrument_token})', lot = {lot}, side={side} | price: {price} | order_link_id: {in_trade_option.order_link_id}"
            print(alert_msg)
            discord.send_alert('cascadeoptions', alert_msg)
            return False

        time.sleep(3)

        order_detail = get_order_detail_with_retries(angel_obj, tp_order_id)

    if order_detail is None:
        alert_msg = f"TP Sell Order created: '{tp_order_id}, Link Id: {in_trade_option.order_link_id} , but failed to retrieve, manually add it."
        print(alert_msg)
        discord.send_alert('cascadeoptions', alert_msg)

    trade_charge = calculate_trade_charge(angel_obj, in_trade_option, (lot * in_trade_option.lot_size),
                                          order_detail['price'], side)

    if order_detail is not None:
        tp_order = Orders(
            symbol=in_trade_option.symbol,
            index=in_trade_option.name,
            token=in_trade_option.instrument_token,
            order_link_id=in_trade_option.order_link_id,
            exchange=in_trade_option.exchange,
            exchange_order_id=tp_order_id,
            price=order_detail['price'],
            lot=lot,
            is_gtt=False,
            quantity=lot * in_trade_option.lot_size,
            fees=trade_charge,
            fees_need_recovery=0,
            side=side,
            type=in_trade_option.instrument_type,
            order_type='TP',
            status='open',
            status_reason=''
        )
        db.session.add(tp_order)
        db.session.commit()
        alert_msg = f"Created LIMIT TP Order '{in_trade_option.symbol}({in_trade_option.instrument_token})' | Lot: {lot}"
        print(alert_msg)
        discord.send_alert('cascadeoptions', alert_msg)
        return tp_order_id
    else:
        alert_msg = f"FAILED LIMIT Order Get: TP order created '{in_trade_option.symbol}({in_trade_option.instrument_token})' | TP Order ID: {tp_order_id}, Lot: {lot} | TP price: {price} | order_link_id: {in_trade_option.order_link_id}"
        print(alert_msg)
        discord.send_alert('cascadeoptions', alert_msg)
        return None


def execute_buy_order(angel_obj, hedge_option, max_lots):
    buy_order_id = place_option_order(angel_obj, hedge_option, 'BUY', max_lots)
    if buy_order_id is None:
        send_alert(
            f"Enter Hedge Buy order for '{hedge_option.symbol}({hedge_option.instrument_token})', but order failed, lot = {max_lots}")
        return False

    time.sleep(3)
    hedge_order_detail = get_order_detail_with_retries(angel_obj, buy_order_id)
    if not hedge_order_detail:
        send_alert(
            f"Buy Hedge Order created: '{buy_order_id}, Link Id: {hedge_option.order_link_id} , but failed to retrieve, manually add it.")

    if hedge_order_detail is None:
        alert_msg = f"Buy Hedge Order created: '{buy_order_id}, Link Id: {hedge_option.order_link_id} , but failed to retrieve, manually add it."
        print(alert_msg)
        discord.send_alert('cascadeoptions', alert_msg)
        return False

    if hedge_order_detail is not None and hedge_order_detail['status'] in ["complete"]:
        # Calculate fees
        hedge_trade_charge = calculate_trade_charge(angel_obj, hedge_option,
                                                    (max_lots * hedge_option.lot_size),
                                                    hedge_order_detail['averageprice'], "SELL")

        hedge_order = create_order_entry(hedge_option, buy_order_id, hedge_order_detail['averageprice'], max_lots,
                                         hedge_trade_charge, "SELL", "HEDGE", "COMPLETE", 0)

    return hedge_order_detail


def calculate_tp_price(lot, price, tp=0, lot_size=15):
    total_value = lot * lot_size * price
    print(total_value)
    points_to_be_captured = tp / (lot * lot_size)
    print(points_to_be_captured)
    # percentage_needed = (points_to_be_captured + total_value) / total_value * 100
    tp_price = price - points_to_be_captured
    if tp_price < 0:
        tp_price = 0.10
    return tp_price


def execute_sell_order(angel_obj, option, max_lots):
    sell_order_id = place_option_order(angel_obj, option, 'SELL', max_lots)
    if sell_order_id is None:
        send_alert(
            f"Enter Sell order for '{option.symbol}({option.instrument_token})', but order failed, lot = {max_lots}")
        return False

    time.sleep(3)
    sell_order_detail = get_order_detail_with_retries(angel_obj, sell_order_id)

    if sell_order_detail['status'] in ["rejected"]:
        send_alert(
            f"Sell order rejected ({option.instrument_token}): {sell_order_detail['text']}")
        return False

    if not sell_order_detail:
        send_alert(
            f"Sell Order created: '{sell_order_id}, Link Id: {option.order_link_id} , but failed to retrieve, manually add it.")
        return False

    if sell_order_detail is not None and sell_order_detail['status'] in ["complete"]:
        # Calculate fees
        trade_charge = calculate_trade_charge(angel_obj, option,
                                              (max_lots * option.lot_size),
                                              sell_order_detail['averageprice'], "SELL")

        main_order = create_order_entry(option, sell_order_id, sell_order_detail['averageprice'], max_lots,
                                        trade_charge, "SELL", "MAIN", "COMPLETE",
                                        0)

        # main_order.entry_candle_index = small_candle_index
        db.session.commit()

    return [sell_order_detail, main_order]


def place_option_order(angel_obj, option, side, lots):
    return angel.place_option_order(angel_obj, 'MARKET', option.symbol, option.instrument_token, side,
                                    lots * option.lot_size, exchange=option.exchange)


def get_order_detail_with_retries(angel_obj, order_id, max_retries=3):
    for attempt in range(max_retries + 1):
        order_detail = angel.get_order_detail(angel_obj, order_id)
        if order_detail is not None:
            return order_detail
        if attempt < max_retries:
            angel_obj = angel.get_angel_obj()  # Fetch new angel_obj if more retries are allowed
    return None


def calculate_trade_charge(angel_obj, in_trade_option, qty, price, transaction_type):
    try:
        params = {
            "orders": [
                {
                    "product_type": config.PRODUCT_TYPE,
                    "transaction_type": transaction_type,
                    "quantity": qty,
                    "price": price,
                    "exchange": in_trade_option.exchange,
                    "symbol_name": in_trade_option.name,
                    "token": str(in_trade_option.instrument_token)
                }
            ]
        }

        charges = angel_obj.estimateCharges(params)
        return charges['data']['summary']['total_charges']
    except Exception as e:
        print(params)
        alert_msg = f"Estimation API failed"
        print(alert_msg)
        discord.send_alert('cascadeoptions', alert_msg)
        return 0


def create_order_entry(in_trade_option, exchange_order_id, price, lot, trade_charge, side, type, status,
                       fund_available):
    order = Orders(
        symbol=in_trade_option.symbol,
        token=in_trade_option.instrument_token,
        order_link_id=in_trade_option.order_link_id,
        exchange=in_trade_option.exchange,
        index=in_trade_option.name,
        exchange_order_id=exchange_order_id,
        price=price,
        lot=lot,
        quantity=lot * in_trade_option.lot_size,
        fees=trade_charge,
        fees_need_recovery=trade_charge,
        type=in_trade_option.instrument_type,
        side=side,
        order_type=type,
        balance_before_trade=fund_available,
        status=status
    )
    db.session.add(order)
    db.session.commit()
    return order


def get_atm_strike(angel_obj, index):
    timeframe = '3m'
    [nse_interval, nse_max_days_per_interval, is_custom_interval] = angel.get_angel_timeframe_details(timeframe)
    df_option = angel.get_historical_data(angel_obj, index.token, timeframe, nse_interval, 750)
    return round_to_nearest(df_option.iloc[-1]['close'], index.option_sizing)


def get_selected_strikes(atm_strike, option_sizing, option_type):
    selected_strikes = []
    for i in range(config.STRIKE_SELECTION_FROM_ITM, config.STRIKE_SELECTION_TO_ITM):
        selected_strikes.append(atm_strike - (option_sizing * i) if option_type == "CE" else atm_strike + (
            option_sizing * i))
    return selected_strikes


def get_otm_options(option_type, atm_strike):
    order_by_clause = asc(Options.strike) if option_type == 'CE' else desc(Options.strike)
    query = Options.query.filter(
        and_(
            Options.instrument_type == option_type,
            Options.strike > atm_strike if option_type == 'CE' else Options.strike < atm_strike
        )
    ).order_by(order_by_clause)

    results = query.all() if option_type == 'CE' else query.all()

    return results


def chunked(iterable, size):
    """Yield successive chunks of size `size` from `iterable`."""
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]


def get_closest_hedge_option(angel_obj, otm_instrument_tokens):
    market_data_frames = []

    for token_chunk in chunked(otm_instrument_tokens, 50):
        exchange_tokens = {"NFO": token_chunk}
        response = angel_obj.getMarketData("LTP", exchange_tokens)
        fetched_data = response.get('data', {}).get('fetched', [])
        if fetched_data:
            market_data_frames.append(pd.DataFrame(fetched_data))

    # Combine all the data
    if not market_data_frames:
        raise ValueError("No market data fetched.")

    market_data = pd.concat(market_data_frames, ignore_index=True)
    market_data['diff'] = abs(market_data['ltp'] - config.HEDGE_LTP)

    closest_hedge = market_data.loc[market_data['diff'].idxmin()]

    try:
        hedge_option = Options.query.filter_by(instrument_token=closest_hedge['symbolToken']).first()
    except NoResultFound:
        hedge_option = None

    return closest_hedge, hedge_option


def get_margin_required(angel_obj, option):
    time.sleep(2)
    main_sell_ltp = pd.DataFrame(angel_obj.getMarketData("LTP", {"NFO": [option.instrument_token]})['data']['fetched'])['ltp'][0]
    # Buffer added to minimize shortage of fund
    main_sell_ltp = main_sell_ltp + 3

    print(option.symbol)
    margin_params = {"positions": [
        {"exchange": "NFO", "qty": option.lot_size, "price": main_sell_ltp, "productType": "CARRYFORWARD",
         "token": str(option.instrument_token), "tradeType": "SELL", "orderType": "MARKET"}
    ]}

    margin_required = angel_obj.getMarginApi(margin_params)['data']['totalMarginRequired']

    print(margin_required)
    return margin_required


def calculate_max_lots(margin_required):
    fund_available = float(angel.get_angel_obj().rmsLimit()['data']['utilisedpayout'])
    usable_fund = fund_available * (1 - config.BUFFER_PERCENTAGE)
    lots = int(usable_fund // margin_required)

    lots = lots - 1 if lots > 1 else lots
    lots = config.MAX_SELL_LOT if lots > config.MAX_SELL_LOT else lots
    return lots


def send_alert(message):
    print(message)
    discord.send_alert('cascadeoptions', message)


def round_to_nearest(number, multiple):
    return round(number / multiple) * multiple


def generate_random_digit_number(n):
    if n <= 0:
        raise ValueError("Number of digits must be greater than 0")
    lower_bound = 10 ** (n - 1)  # Smallest n-digit number
    upper_bound = 10 ** n - 1  # Largest n-digit number
    return random.randint(lower_bound, upper_bound)


@click.command(name='margin')
def margin():
    angel_obj = angel.get_angel_obj()
    index = Indexes.query.filter_by(enabled=True).first()
    atm_strike = get_atm_strike(angel_obj, index)
    selected_strike = get_selected_strikes(atm_strike, index.option_sizing, 'CE')
    option = Options.query.filter_by(instrument_type='CE', strike=selected_strike, name=index.name).first()
    otm_options = get_otm_options('CE', atm_strike)
    otm_instrument_tokens = [otm_option.instrument_token for otm_option in otm_options]
    closest_hedge, hedge_option = get_closest_hedge_option(angel_obj, otm_instrument_tokens)

    if not hedge_option:
        return

    margin_required = get_margin_required(angel_obj, option, closest_hedge)
    print(f"Margin required: {margin_required}")


app.cli.add_command(check_entry)
app.cli.add_command(margin)
