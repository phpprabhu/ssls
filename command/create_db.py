from sslsapp import app, db
import click
from flask.cli import with_appcontext
from sslsapp.models.model import TradeSettings, Indexes, LastRun, Loss, Orders, DciEarnings, Cookie
from command.tokens import fetch_option_token, update_near_token
import exchange.angel as angel
import math
from datetime import datetime, timedelta
import config


@click.command(name='restart')
@click.pass_context
def restart(ctx):
    ce_orders = Orders.query.filter_by(type='CE', status='COMPLETE').all()
    total_ce_loss_recovery = sum(order.loss_need_recovery for order in ce_orders)
    total_ce_fees_recovery = sum(order.fees_need_recovery for order in ce_orders)

    pe_orders = Orders.query.filter_by(type='PE', status='COMPLETE').all()
    total_pe_loss_recovery = sum(order.loss_need_recovery for order in pe_orders)
    total_pe_fees_recovery = sum(order.fees_need_recovery for order in pe_orders)

    db.drop_all()
    click.echo('Database deleted successfully!')
    ctx.invoke(create_db)
    ctx.invoke(create_achievement, days=100, interest_rate=1.0, investment=0)
    ctx.invoke(fetch_option_token)
    ctx.invoke(update_near_token)

    create_order_entry('CE', total_ce_fees_recovery, total_ce_loss_recovery)
    create_order_entry('PE', total_pe_fees_recovery, total_pe_loss_recovery)


def create_order_entry(order_type, fees_need_recovery, loss_need_recovery):
    order = Orders(
        symbol="PREVIOUS" + order_type,
        token=12345,
        order_link_id=1234567890,
        exchange='NFO',
        index='PREV',
        exchange_order_id=1234567890,
        price=100,
        lot=1,
        quantity=0,
        fees=0,
        fees_need_recovery=fees_need_recovery,
        profit=0,
        loss=loss_need_recovery,
        loss_need_recovery=loss_need_recovery,
        type=order_type,
        side='SELL',
        order_type='L-EXIT',
        balance_before_trade=0,
        status='COMPLETE'
    )
    db.session.add(order)
    db.session.commit()


@click.command(name='create-db')
@with_appcontext
def create_db():
    db.create_all()

    indexes = Indexes(symbol='Midcap Nifty', name='MIDCPNIFTY', token=99926009, type='AMXIDX', exp='M', enabled=False, lot_size=120, exp_day='mon', topic="1.99926074", option_sizing=25)
    db.session.add(indexes)
    indexes = Indexes(symbol='Nifty Fin Service', name='FINNIFTY', token=99926037, type='AMXIDX', exp='M', enabled=False,
                      lot_size=65, exp_day='tue', topic="1.99926037", option_sizing=50)
    db.session.add(indexes)
    indexes = Indexes(symbol='Nifty Bank', name='BANKNIFTY', token=99926009, type='AMXIDX', enabled=False, lot_size=30, exp='M', exp_day='wed', topic="1.99926009", option_sizing=100)
    db.session.add(indexes)
    indexes = Indexes(symbol='Nifty', name='NIFTY', token=99926000, type='AMXIDX', enabled=True, lot_size=75, exp='W', exp_day='thu', topic="1.99926000", option_sizing=50)
    db.session.add(indexes)
    indexes = Indexes(symbol='Sensex', name='SENSEX', token=99926009, type='AMXIDX', enabled=False, exp='W', lot_size=20,
                      exp_day='fri', topic="3.99919000", exchange="BFO", option_sizing=100)
    db.session.add(indexes)

    trade_settings = TradeSettings(tp_percentage=20, sl_percentage=10, risk_percentage=1, lot=1, demo=0)
    db.session.add(trade_settings)

    last_ran = LastRun(cron='ALL-OPTIONS')
    db.session.add(last_ran)

    last_ran = LastRun(cron='NEAR')
    db.session.add(last_ran)

    db.session.commit()

    print('Created')


@click.command(name='create_achievement')
@click.argument('days')
@click.argument('interest_rate')
@click.argument('investment')
def create_achievement(days, interest_rate, investment=0):
    investment = float(investment)
    balance = investment
    if investment == 0:
        angel_obj = angel.get_angel_obj()
        profile = angel_obj.rmsLimit()['data']
        fund_available = float(profile['utilisedpayout'])
        balance = math.floor(fund_available)

    DciEarnings.query.delete()

    # Convert string holidays to datetime.date objects
    holidays = [datetime.strptime(date, '%Y-%m-%d').date() for date in config.HOLIDAYS]

    day = 1
    current_date = datetime.utcnow().date()

    while day <= int(days):
        interest = round(balance*float(interest_rate)/100, 2)
        balance = balance + interest
        print(day)
        print(interest)
        dci_earning = DciEarnings(day=day, earnings=interest)
        db.session.add(dci_earning)

        day += 1
        current_date += timedelta(days=1)

    db.session.commit()


@click.command(name='add-cookie')
@click.argument('cookie')
def add_cookie(cookie):
    cookie = Cookie(cookie=cookie)
    db.session.add(cookie)
    db.session.commit()


@click.command(name='add-loss')
@click.argument('loss')
@click.argument('loss_date')
def add_loss(loss, loss_date):
    date_obj = datetime.strptime(loss_date, '%Y-%m-%d').date()
    loss = float(loss) * -1
    loss = Loss(loss=loss, date=date_obj)
    db.session.add(loss)
    db.session.commit()


app.cli.add_command(create_achievement)
app.cli.add_command(create_db)
app.cli.add_command(restart)
app.cli.add_command(add_cookie)
app.cli.add_command(add_loss)