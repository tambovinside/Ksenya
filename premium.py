"""Telegram Stars Premium: subscriptions, recurring rewards and administration."""
import logging, os
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from ui_design import InlineKeyboardButton

import accounts
import db

log = logging.getLogger(__name__)
try:
 ADMIN_ID = int(os.getenv('ADMIN_ID', '0') or 0)
except (TypeError, ValueError):
 log.warning('Некорректный ADMIN_ID: административные команды отключены')
 ADMIN_ID = 0
PLANS = {
 'pro': {'title':'Premium Pro 💎','days':29,'price':12,'coins':400,'daily_coins':25,'tokens':1000,'daily_tokens':10,'renew':10,'badge':'💎','packs':'all'},
 'base': {'title':'Premium Base 🤑','days':25,'price':6,'coins':100,'daily_coins':10,'tokens':100,'daily_tokens':0,'renew':5,'badge':'🤑','packs':'one'},
}

async def init_db():
 statements = [
  """CREATE TABLE IF NOT EXISTS premium_subscriptions (
      user_id INTEGER PRIMARY KEY, plan TEXT NOT NULL, expires_at TEXT NOT NULL,
      last_reward_day TEXT, base_pocket_id INTEGER,
      free_credit_used INTEGER NOT NULL DEFAULT 0)""",
  """CREATE TABLE IF NOT EXISTS premium_payments (
      charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, plan TEXT NOT NULL,
      created_at TEXT NOT NULL)""",
  """CREATE TABLE IF NOT EXISTS coin_shop_payments (
      charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, coins INTEGER NOT NULL,
      created_at TEXT NOT NULL)""",
  """CREATE TRIGGER IF NOT EXISTS premium_daily_reward
      AFTER UPDATE OF last_reward_day ON premium_subscriptions
      WHEN OLD.last_reward_day IS NOT NEW.last_reward_day
      BEGIN
        UPDATE users SET
          balance = COALESCE(balance, 10) +
            CASE NEW.plan WHEN 'pro' THEN 25 WHEN 'base' THEN 10 ELSE 0 END,
          ai_tokens = COALESCE(ai_tokens, 0) +
            CASE NEW.plan WHEN 'pro' THEN 10 ELSE 0 END
        WHERE user_id = NEW.user_id;
      END"""
 ]
 for sql in statements:
  result = await db._exec(sql)
  if getattr(result, 'error', False):
   raise RuntimeError('Не удалось инициализировать таблицы Premium')


async def get(user_id):
 r = await db._exec(
  'SELECT plan, expires_at, base_pocket_id FROM premium_subscriptions WHERE user_id=?',
  [int(user_id)]
 )
 if getattr(r, 'error', False) or not r.rows:
  return None
 p, e, b = r.rows[0]
 try:
  expiry = datetime.fromisoformat(str(e))
  if expiry.tzinfo is None:
   expiry = expiry.replace(tzinfo=timezone.utc)
  active = expiry > datetime.now(timezone.utc)
 except (TypeError, ValueError, OverflowError):
  active = False
 return {'plan': p, 'expires_at': str(e), 'base_pocket_id': b, 'active': True} if active else None


async def has_protection(user_id): return bool(await get(user_id))

async def open_menu(q):
 await q.edit_message_text('🛍 <b>МАГАЗИН</b>\n\nВыберите, что хотите приобрести:',parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🪙 Купить монеты за Stars',callback_data='shop:coins')],[InlineKeyboardButton('⭐ Premium-подписка',callback_data='shop:premium')],[InlineKeyboardButton('← Назад',callback_data='shop:main')]]))
 await q.answer()

async def show_catalog(q):
 await q.edit_message_text('⭐ <b>PREMIUM</b>\n\nВыберите подписку:',parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💎 Premium Pro',callback_data='shop:premium_plan:pro')],[InlineKeyboardButton('🤑 Premium Base',callback_data='shop:premium_plan:base')],[InlineKeyboardButton('← В магазин',callback_data='shop:premium_home')]]))
 await q.answer()

async def show_plan(q, plan):
 p=PLANS[plan]
 if plan=='pro':
  perks='''1. 1 бесплатное полное списание всех кредитов\n2. Защита от дефолтов\n3. +25 монет ежедневно\n4. +400 монет сразу\n5. +1000 токенов ИИ сразу\n6. +10 токенов ИИ ежедневно\n7. Ускоренное восстановление (30 минут)\n8. Статус Premium Pro 💎 в профиле\n9. 💎 рядом с ником в топах\n10. Все платные паки команд без ограничений'''
 else:
  perks='''1. Защита от дефолтов\n2. +100 монет сразу\n3. +10 монет ежедневно\n4. +100 токенов ИИ сразу\n5. 🤑 рядом с ником в топах\n6. Статус Premium Base 🤑 в профиле\n7. Один платный пак команд на выбор'''
 text=f"<b>{p['title']}</b>\n\nЦена: <b>{p['price']}⭐</b>\nСрок действия: <b>{p['days']} дней</b>{' + 2🎁' if plan=='pro' else ''}\n\n<b>Разблокируется:</b>\n{perks}"
 await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Оплатить {p['price']} ⭐",callback_data=f'shop:premium_pay:{plan}')],[InlineKeyboardButton('← Каталог',callback_data='shop:premium')]]))
 await q.answer()

async def choose_base_pack(q):
 pockets=await db.shop_pockets_list(q.message.chat.id)
 rows=[]
 for item in pockets or []:
  if item.get('active'):
   rows.append([InlineKeyboardButton(f"{item['name']} · {item['price']}🪙",callback_data=f"shop:premium_pack:{int(item['id'])}")])
 rows.append([InlineKeyboardButton('← Назад',callback_data='shop:premium_plan:base')])
 await q.edit_message_text('🤑 <b>Выберите один платный пакет</b>, который будет доступен на время Premium Base:',parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))
 await q.answer()

async def send_invoice(q, context, user, plan):
 p=PLANS[plan]
 owner_id = await accounts.data_id_for(user.id)
 payload=f"premium:{plan}:{owner_id}:{int(datetime.now(timezone.utc).timestamp())}:{int(context.user_data.get('premium_base_pack', 0) or 0)}"
 await context.bot.send_invoice(chat_id=q.message.chat_id,title=p['title'],description=f"Подписка на {p['days']} дней",payload=payload,provider_token='',currency='XTR',prices=[LabeledPrice(p['title'],p['price'])])
 await q.answer('Счёт отправлен. Оплати его сообщением выше.')

async def precheckout(update:Update, context:ContextTypes.DEFAULT_TYPE):
 q = update.pre_checkout_query
 if not q:
  return
 parts = (q.invoice_payload or '').split(':')
 ok = False
 try:
  now_ts = int(datetime.now(timezone.utc).timestamp())
  expected_owner = await accounts.data_id_for(q.from_user.id)
  if len(parts) == 4 and parts[0] == 'coins':
   owner, coins, issued = int(parts[1]), int(parts[2]), int(parts[3])
   ok = (
    owner == expected_owner and 20 <= coins <= 200000 and coins % 20 == 0
    and 0 <= now_ts - issued <= 86400
    and q.currency == 'XTR' and q.total_amount == coins // 20
   )
  elif len(parts) == 5 and parts[0] == 'premium' and parts[1] in PLANS:
   owner, issued, pack_id = int(parts[2]), int(parts[3]), int(parts[4])
   ok = (
    owner == expected_owner and 0 <= now_ts - issued <= 86400
    and q.currency == 'XTR'
    and q.total_amount == PLANS[parts[1]]['price']
    and ((parts[1] == 'base' and pack_id >= 0) or (parts[1] == 'pro' and pack_id == 0))
   )
   if ok and pack_id:
    pocket = await db.shop_pocket_get(pack_id)
    ok = bool(pocket and pocket.get('active'))
 except (TypeError, ValueError, IndexError):
  ok = False
 except Exception:
  log.exception('Ошибка проверки pre-checkout')
  ok = False
 await q.answer(
  ok=ok,
  error_message=None if ok else 'Счёт недействителен или устарел. Открой магазин и создай новый.'
)


async def activate(user, plan, context, charge_id, chat_id=None, base_pocket_id=None):
 if plan not in PLANS or not charge_id:
  return False
 p = PLANS[plan]
 try:
  if base_pocket_id is not None:
   if plan != 'base':
    return False
   pocket = await db.shop_pocket_get(int(base_pocket_id))
   if not pocket or not pocket.get('active'):
    return False

  # The charge is reserved first to prevent duplicate grants on repeated updates.
  # Any later failure is logged for manual reconciliation.
  ins = await db._exec(
   'INSERT OR IGNORE INTO premium_payments(charge_id,user_id,plan,created_at) VALUES(?,?,?,?)',
   [str(charge_id), user.id, plan, datetime.now(timezone.utc).isoformat()]
  )
  if getattr(ins, 'error', False) or not getattr(ins, 'rows_affected', 0):
   return False

  old = await get(user.id)
  now = datetime.now(timezone.utc)
  expiry = now + timedelta(days=p['days'])
  if old and old['plan'] == plan:
   try:
    old_expiry = datetime.fromisoformat(old['expires_at'])
    if old_expiry.tzinfo is None:
     old_expiry = old_expiry.replace(tzinfo=timezone.utc)
    expiry = max(now, old_expiry) + timedelta(days=p['days'])
   except (TypeError, ValueError, OverflowError):
    pass

  await db.upsert_user(user.id, user.username, user.first_name or '')
  sub = await db._exec(
   """INSERT INTO premium_subscriptions
      (user_id,plan,expires_at,last_reward_day,base_pocket_id,free_credit_used)
      VALUES(?,?,?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET
       plan=excluded.plan, expires_at=excluded.expires_at,
       last_reward_day=excluded.last_reward_day,
       base_pocket_id=excluded.base_pocket_id""",
   [user.id, plan, expiry.isoformat(), now.date().isoformat(),
    base_pocket_id, 1 if plan == 'pro' else 0]
  )
  if getattr(sub, 'error', False):
   raise RuntimeError('Не удалось сохранить подписку')

  if not await db.add_balance(user.id, p['coins']):
   raise RuntimeError('Не удалось начислить стартовые монеты')
  if p['tokens'] and not await db.refund_ai_tokens(user.id, p['tokens']):
   raise RuntimeError('Не удалось начислить стартовые токены ИИ')

  if plan == 'pro':
   credit = await db._exec('UPDATE users SET credit=0 WHERE user_id=?', [user.id])
   if getattr(credit, 'error', False):
    raise RuntimeError('Не удалось применить защиту Premium Pro')

  safe_username = (user.username or str(user.id)).replace('<', '').replace('>', '')
  receipt = (
   f"🧾 <b>Чек Premium</b>\nКому: @{safe_username}\nОт кого: @YouWarm"
   f"\nЦена: {p['price']}⭐\nУслуга: покупка премиума — {p['title']}"
   f"\nКогда выдано: {now.strftime('%d.%m.%Y %H:%M UTC')}"
   f"\nКогда истечет: {expiry.strftime('%d.%m.%Y %H:%M UTC')}"
  )
  try:
   await context.bot.send_message(user.id, receipt, parse_mode=ParseMode.HTML)
  except Exception:
   log.exception('Не удалось отправить чек покупателю')
  if ADMIN_ID:
   try:
    await context.bot.send_message(ADMIN_ID, receipt, parse_mode=ParseMode.HTML)
   except Exception:
    log.exception('Не удалось отправить чек админу')
  if chat_id:
   try:
    await context.bot.send_message(
     chat_id, f"🎉 {user.mention_html()} активировал(а) {p['title']}!",
     parse_mode=ParseMode.HTML
    )
   except Exception:
    log.exception('Не удалось отправить уведомление о Premium')
  return True
 except Exception:
  log.exception('Ошибка активации Premium: user=%s plan=%s charge=%s', user.id, plan, charge_id)
  return False


async def successful_payment(update:Update,context:ContextTypes.DEFAULT_TYPE):
 msg=update.effective_message; user=await accounts.synthetic_user(update.effective_user)
 sp=msg.successful_payment if msg else None
 if not sp or not user:return
 parts=(sp.invoice_payload or '').split(':')
 if parts and parts[0]=='coins':
  try:
   valid=(len(parts)==4 and int(parts[1])==user.id and int(parts[2])>0 and int(parts[2])%20==0 and sp.currency=='XTR' and sp.total_amount==int(parts[2])//20)
  except (TypeError,ValueError): valid=False
  if not valid:
   log.error('Invalid coin purchase payment payload for user %s',user.id); return
  coins=int(parts[2]); charge=sp.telegram_payment_charge_id
  ins=await db._exec('INSERT OR IGNORE INTO coin_shop_payments(charge_id,user_id,coins,created_at) VALUES(?,?,?,?)',[charge,user.id,coins,datetime.now(timezone.utc).isoformat()])
  if getattr(ins,'error',False):
   log.error('Could not record coin payment %s',charge); return
  if not ins.rows_affected:return
  await db.upsert_user(user.id,user.username,user.first_name or '')
  if not await db.add_balance(user.id,coins):
   log.error('Payment recorded but coin credit failed: user=%s coins=%s charge=%s',user.id,coins,charge); return
  await msg.reply_text(f'✅ Оплата прошла! На баланс начислено {coins} 🪙.')
  return
 if len(parts)!=5 or parts[0]!='premium' or parts[1] not in PLANS:return
 try: valid=int(parts[2])==user.id and sp.currency=='XTR' and sp.total_amount==PLANS[parts[1]]['price']
 except (TypeError,ValueError): valid=False
 if not valid:return
 ok=await activate(user,parts[1],context,sp.telegram_payment_charge_id,chat_id=msg.chat_id,base_pocket_id=(int(parts[4]) or None))
 if ok: await msg.reply_text('✅ Premium активирован! Чек отправлен в личные сообщения.')

async def start_coin_purchase(q, context):
 from telegram import ForceReply
 context.user_data['awaiting_coin_amount']=True
 await q.message.reply_text('🪙 Введите количество монет.\n\nКурс: 1 монета = 0,05 ⭐.\nМинимум — 20 монет (1 ⭐), количество должно делиться на 20.',reply_markup=ForceReply(selective=True))
 await q.answer('Введите количество монет ответом на сообщение бота.')

async def coin_amount_input(update:Update, context:ContextTypes.DEFAULT_TYPE):
 msg=update.effective_message; user=update.effective_user
 if not msg or not user or not context.user_data.get('awaiting_coin_amount') or not msg.reply_to_message:return
 try: coins=int((msg.text or '').strip())
 except ValueError:
  await msg.reply_text('Введите целое число монет, например 100.'); return
 if coins<20 or coins>200000 or coins%20:
  await msg.reply_text('Количество должно быть от 20 до 200 000 и делиться на 20. Например: 100.'); return
 context.user_data.pop('awaiting_coin_amount',None)
 stars=coins//20
 owner_id=await accounts.data_id_for(user.id)
 payload=f'coins:{owner_id}:{coins}:{int(datetime.now(timezone.utc).timestamp())}'
 await context.bot.send_invoice(chat_id=msg.chat_id,title=f'{coins} монет',description=f'Пополнение баланса на {coins} монет по курсу 0,05 ⭐ за монету',payload=payload,provider_token='',currency='XTR',prices=[LabeledPrice(f'{coins} монет',stars)])
 await msg.reply_text(f'🧾 К оплате: {coins} 🪙 × 0,05 ⭐ = {stars} ⭐\nНажмите кнопку оплаты в счёте выше.')

async def rewards_job(context:ContextTypes.DEFAULT_TYPE):
 now = datetime.now(timezone.utc)
 today = now.date().isoformat()
 rs = await db._exec(
  'SELECT user_id,plan,expires_at,last_reward_day FROM premium_subscriptions'
 )
 if getattr(rs, 'error', False):
  log.error('Не удалось получить подписки для ежедневных наград')
  return
 for uid, plan, expires, last in rs.rows:
  try:
   expiry = datetime.fromisoformat(str(expires))
   if expiry.tzinfo is None:
    expiry = expiry.replace(tzinfo=timezone.utc)
   active = expiry > now
  except (TypeError, ValueError, OverflowError):
   active = False

  if not active:
   deleted = await db._exec(
    'DELETE FROM premium_subscriptions WHERE user_id=? AND expires_at=?',
    [uid, expires]
   )
   if getattr(deleted, 'error', False):
    log.error('Не удалось удалить истёкшую подписку user=%s', uid)
    continue
   try:
    plan_cfg = PLANS.get(plan, PLANS['base'])
    plan_key = plan if plan in PLANS else 'base'
    await context.bot.send_message(
     int(uid), '⌛ Срок действия Premium истёк. Продлить премиум?\n',
     reply_markup=InlineKeyboardMarkup([[
      InlineKeyboardButton(
       f"Продлить за {plan_cfg['renew']}⭐",
       callback_data=f'shop:premium_renew:{plan_key}'
      )
     ]])
    )
   except Exception:
    log.exception('Не удалось уведомить об истечении Premium user=%s', uid)
   continue

  # SQLite trigger awards coins/tokens atomically with claiming today's reward.
  if last != today:
   updated = await db._exec(
    """UPDATE premium_subscriptions SET last_reward_day=?
       WHERE user_id=? AND last_reward_day IS NOT ? AND expires_at>?""",
    [today, uid, today, now.isoformat()]
   )
   if getattr(updated, 'error', False):
    log.error('Не удалось начислить ежедневную награду user=%s', uid)


async def admin_command(update:Update,context:ContextTypes.DEFAULT_TYPE):
 msg = update.effective_message
 actor = update.effective_user
 if not msg:
  return
 if not actor or not ADMIN_ID or actor.id != ADMIN_ID:
  await msg.reply_text('⛔ Только для администратора.')
  return
 if len(context.args) != 4 or context.args[0] not in ('+', '-') or context.args[1].lower() not in PLANS:
  await msg.reply_text('Формат: /GetPremium +/- Base/Pro количество_дней @username')
  return
 op, plan, days_raw, target = context.args
 plan = plan.lower()
 uid = await db.get_user_by_username(target.lstrip('@'))
 if not uid:
  await msg.reply_text('Пользователь не найден в базе.')
  return
 if op == '-':
  result = await db._exec(
   'DELETE FROM premium_subscriptions WHERE user_id=? AND plan=?',
   [uid, plan]
  )
  if getattr(result, 'error', False):
   await msg.reply_text('Не удалось снять Premium. Проверь логи.')
  else:
   await msg.reply_text('Premium снят.')
  return
 try:
  days = int(days_raw)
 except (TypeError, ValueError):
  await msg.reply_text('Количество дней должно быть целым числом.')
  return
 if days < 1 or days > 3650:
  await msg.reply_text('Укажи от 1 до 3650 дней.')
  return
 now = datetime.now(timezone.utc)
 result = await db._exec(
  """INSERT INTO premium_subscriptions(user_id,plan,expires_at,last_reward_day)
     VALUES(?,?,?,?)
     ON CONFLICT(user_id) DO UPDATE SET
      plan=excluded.plan, expires_at=excluded.expires_at,
      last_reward_day=excluded.last_reward_day""",
  [uid, plan, (now + timedelta(days=days)).isoformat(), now.date().isoformat()]
 )
 if getattr(result, 'error', False):
  await msg.reply_text('Не удалось выдать Premium. Проверь логи.')
  return
 await msg.reply_text(f'✅ {plan} выдан на {days} дней пользователю @{target.lstrip("@")}')
