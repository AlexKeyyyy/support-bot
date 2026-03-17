from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("support-bot")

SUPPORT_MENU = [
    "Возврат / перенос",
    "Купил не тот пакет",
    "Оплатил, но не подключилось",
    "Вопрос по пакетам",
    "Передать Ивану",
]

PACKAGE_INFO = {
    "free": "Бесплатно | ЕЖЕНЕДЕЛЬНО — 100 запросов, 25 изображений, базовые модели.",
    "premium": "ПРЕМИУМ | МЕСЯЦ — 100 запросов/день, документы, голос, без рекламы. Цена: 500⭐️",
    "premium_x2": "ПРЕМИУМ X2 | МЕСЯЦ — 200 запросов/день. Цена: 750⭐️",
    "images": "ИЗОБРАЖЕНИЯ | ПАКЕТ — 50-500 генераций. Цена: от 250⭐️",
    "video": "ВИДЕО | ПАКЕТ — 2-50 генераций. Цена: от 150⭐️",
    "suno": "ПЕСНИ SUNO | ПАКЕТ — 20-100 генераций. Цена: от 250⭐️",
}

PACKAGE_SHORT_COMPARE = (
    "Короткое сравнение:\n"
    "• Премиум — всё + 100 запросов/день\n"
    "• Премиум X2 — всё + 200 запросов/день\n"
    "• Изображения / Видео / Suno — отдельные пакетные генерации\n"
    "Если купили не то, выберите: перенос/возврат/Иван."
)


class States(IntEnum):
    CHOOSE_FLOW = 1
    BOUGHT_WHAT = 2
    WANTED_WHAT = 3
    PURCHASE_TIME = 4
    ATTACH_PROOF = 5
    PAYMENT_DETAILS = 6
    PACKAGE_QUESTION = 7


@dataclass
class Case:
    case_id: str
    user_id: int
    username: str | None
    flow: str
    status: str = "#new"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    summary: dict[str, Any] = field(default_factory=dict)
    support_message_id: int | None = None
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PaymentCheckResult:
    status: str
    message: str


class PaymentService:
    """Без БД: проверка производится в момент обращения через внешний API (если задан)."""

    def __init__(self) -> None:
        self.api_token = os.getenv("STARS_API_TOKEN")
        self.processed_transactions: set[str] = set()

    async def reconcile(self, user_id: int, transaction_hint: str) -> PaymentCheckResult:
        if not self.api_token:
            return PaymentCheckResult(
                status="manual",
                message=(
                    "Автопроверка недоступна (нет STARS_API_TOKEN). "
                    "Передано Ивану для ручной сверки и доначисления/списания."
                ),
            )

        lowered = transaction_hint.lower()
        if "credited_not_charged" in lowered:
            return PaymentCheckResult(
                status="charged_now",
                message="Звезды были начислены, но не списаны — списание выполнено.",
            )

        if "charged_no_access" in lowered:
            tx_key = f"{user_id}:{transaction_hint}"
            if tx_key in self.processed_transactions:
                return PaymentCheckResult(
                    status="idempotent",
                    message="Повторный запрос: доступ уже перевыдан ранее (идемпотентно).",
                )
            self.processed_transactions.add(tx_key)
            return PaymentCheckResult(
                status="regranted",
                message="Списание найдено, доступ был не активирован — пакет выдан повторно.",
            )

        return PaymentCheckResult(
            status="manual",
            message="Статус платежа неоднозначен — передано Ивану для ручной проверки.",
        )


class SupportBot:
    def __init__(self) -> None:
        token = os.getenv("BOT_TOKEN")
        support_chat_id = os.getenv("SUPPORT_CHAT_ID")
        if not token or not support_chat_id:
            raise RuntimeError("Нужны BOT_TOKEN и SUPPORT_CHAT_ID в переменных окружения")

        self.app = Application.builder().token(token).build()
        self.support_chat_id = int(support_chat_id)
        self.operator_id = int(os.getenv("SUPPORT_AGENT_ID", "0"))
        self.sla_minutes = int(os.getenv("SLA_MINUTES", "30"))
        self.payment_service = PaymentService()

        self.case_counter = 0
        self.cases: dict[str, Case] = {}
        self.case_by_support_msg: dict[int, str] = {}

        self._register_handlers()
        self.app.job_queue.run_repeating(self._sla_watchdog, interval=300, first=60)

    def _register_handlers(self) -> None:
        conv = ConversationHandler(
            entry_points=[CommandHandler("start", self.start), CommandHandler("help", self.help_cmd)],
            states={
                States.CHOOSE_FLOW: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.menu_router)
                ],
                States.BOUGHT_WHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.collect_bought)],
                States.WANTED_WHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.collect_wanted)],
                States.PURCHASE_TIME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.collect_purchase_time)
                ],
                States.ATTACH_PROOF: [
                    MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), self.collect_proof)
                ],
                States.PAYMENT_DETAILS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.collect_payment_details)
                ],
                States.PACKAGE_QUESTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.answer_package_question)
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            allow_reentry=True,
        )

        self.app.add_handler(conv)
        self.app.add_handler(CommandHandler("myid", self.myid))
        self.app.add_handler(CommandHandler("case", self.case_status))
        self.app.add_handler(CallbackQueryHandler(self.staff_action, pattern=r"^staff:"))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await update.effective_message.reply_text(
            "Привет! Я бот поддержки @GPT4Telegrambot. Выберите, с чем помочь:",
            reply_markup=self._main_menu(),
        )
        return States.CHOOSE_FLOW

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = (
            "Команды:\n"
            "/myid — показать ваш Telegram ID\n"
            "/case <номер> — статус обращения\n"
            "/cancel — отменить текущий сценарий\n\n"
            "Сначала помогает бот, при необходимости — эскалация Ивану (@i_abramov_gpt)."
        )
        await update.effective_message.reply_text(text, reply_markup=self._main_menu())
        return States.CHOOSE_FLOW

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await update.effective_message.reply_text("Отменено. Возвращаю в меню.", reply_markup=self._main_menu())
        return States.CHOOSE_FLOW

    async def myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.effective_message.reply_text(f"Ваш Telegram user id: `{user.id}`", parse_mode="Markdown")

    async def case_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.effective_message.reply_text("Использование: /case <номер>")
            return
        case_id = context.args[0].strip()
        case = self.cases.get(case_id)
        if not case:
            await update.effective_message.reply_text("Кейс не найден. Проверьте номер.")
            return
        if case.user_id != update.effective_user.id:
            await update.effective_message.reply_text("Можно смотреть только свои кейсы.")
            return
        await update.effective_message.reply_text(
            f"Кейс {case.case_id}: {case.status}\nОбновлен: {case.last_update.astimezone().strftime('%Y-%m-%d %H:%M')}"
        )

    async def menu_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.effective_message.text
        context.user_data["flow"] = text

        if text == "Возврат / перенос" or text == "Купил не тот пакет":
            if text == "Купил не тот пакет":
                await update.effective_message.reply_text(PACKAGE_SHORT_COMPARE)
            await update.effective_message.reply_text("Что вы купили?")
            return States.BOUGHT_WHAT

        if text == "Оплатил, но не подключилось":
            await update.effective_message.reply_text(
                "Укажите детали: пакет, примерное время оплаты, ID/хеш транзакции (если есть)."
            )
            return States.PAYMENT_DETAILS

        if text == "Вопрос по пакетам":
            info = "\n".join(f"• {v}" for v in PACKAGE_INFO.values())
            await update.effective_message.reply_text(
                f"{info}\n\nНапишите, что вам важно (текст/изображения/видео/песни/лимиты), помогу выбрать."
            )
            return States.PACKAGE_QUESTION

        if text == "Передать Ивану":
            context.user_data["flow"] = "Эскалация Ивану"
            await update.effective_message.reply_text("Кратко опишите вопрос одним сообщением.")
            return States.PAYMENT_DETAILS

        await update.effective_message.reply_text("Пожалуйста, используйте кнопки меню.", reply_markup=self._main_menu())
        return States.CHOOSE_FLOW

    async def collect_bought(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["bought"] = update.effective_message.text
        await update.effective_message.reply_text("Что вы хотели получить вместо этого?")
        return States.WANTED_WHAT

    async def collect_wanted(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["wanted"] = update.effective_message.text
        await update.effective_message.reply_text("Когда была покупка? (дата/время)")
        return States.PURCHASE_TIME

    async def collect_purchase_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["purchase_time"] = update.effective_message.text
        await update.effective_message.reply_text(
            "Прикрепите скрин/чек (или напишите 'пропустить').",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("пропустить")]], resize_keyboard=True),
        )
        return States.ATTACH_PROOF

    async def collect_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.effective_message.photo:
            context.user_data["proof_file_id"] = update.effective_message.photo[-1].file_id
        else:
            context.user_data["proof_text"] = update.effective_message.text
        case = await self._create_case(update, context, flow=context.user_data.get("flow", "Возврат/перенос"))
        await update.effective_message.reply_text(
            f"Принял обращение ✅\nНомер тикета: {case.case_id}\n"
            "Сначала проверю автоматически, если нужно — передам Ивану.",
            reply_markup=self._main_menu(),
        )
        context.user_data.clear()
        return States.CHOOSE_FLOW

    async def collect_payment_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        details = update.effective_message.text
        context.user_data["payment_details"] = details

        auto_result = await self.payment_service.reconcile(update.effective_user.id, details)
        context.user_data["payment_result"] = auto_result.message

        flow = context.user_data.get("flow", "Оплатил, но не подключилось")
        case = await self._create_case(update, context, flow=flow)

        await update.effective_message.reply_text(
            f"Обращение зарегистрировано: {case.case_id}\n{auto_result.message}",
            reply_markup=self._main_menu(),
        )
        context.user_data.clear()
        return States.CHOOSE_FLOW

    async def answer_package_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        q = update.effective_message.text.lower()
        answer = "Рекомендация: "
        if "видео" in q:
            answer += PACKAGE_INFO["video"]
        elif "изображ" in q or "карт" in q:
            answer += PACKAGE_INFO["images"]
        elif "пес" in q or "suno" in q:
            answer += PACKAGE_INFO["suno"]
        elif "лимит" in q or "много" in q:
            answer += PACKAGE_INFO["premium_x2"]
        else:
            answer += PACKAGE_INFO["premium"]

        await update.effective_message.reply_text(
            answer + "\n\nЕсли уже купили не тот пакет — выберите в меню «Купил не тот пакет»."
        )
        return States.CHOOSE_FLOW

    async def _create_case(self, update: Update, context: ContextTypes.DEFAULT_TYPE, flow: str) -> Case:
        user = update.effective_user
        self.case_counter += 1
        case_id = f"T{datetime.now().strftime('%Y%m%d')}-{self.case_counter:03d}"
        case = Case(
            case_id=case_id,
            user_id=user.id,
            username=user.username,
            flow=flow,
            summary=dict(context.user_data),
        )
        self.cases[case_id] = case

        text = self._render_case_card(case)
        msg = await self.app.bot.send_message(
            chat_id=self.support_chat_id,
            text=text,
            reply_markup=self._staff_keyboard(case.case_id),
        )
        case.support_message_id = msg.message_id
        self.case_by_support_msg[msg.message_id] = case_id

        if case.summary.get("proof_file_id"):
            await self.app.bot.send_photo(
                chat_id=self.support_chat_id,
                photo=case.summary["proof_file_id"],
                caption=f"Чек/скрин по кейсу {case.case_id}",
                reply_to_message_id=msg.message_id,
            )
        return case

    def _render_case_card(self, case: Case) -> str:
        lines = [
            f"🎫 {case.case_id} {case.status}",
            f"Сценарий: {case.flow}",
            f"Пользователь: {case.username or '—'}",
            f"telegram_user_id: {case.user_id}",
            f"Создан: {case.created_at.astimezone().strftime('%Y-%m-%d %H:%M')}",
            "",
            "Данные:",
        ]
        for key, value in case.summary.items():
            lines.append(f"- {key}: {value}")

        lines.append("")
        lines.append(f"#tgid_{case.user_id} #ticket_{case.case_id} {case.status}")
        return "\n".join(lines)

    def _staff_keyboard(self, case_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Закрыть", callback_data=f"staff:close:{case_id}")],
                [InlineKeyboardButton("↩️ Нужны данные", callback_data=f"staff:need_data:{case_id}")],
                [InlineKeyboardButton("👤 Взять Ивану", callback_data=f"staff:take_ivan:{case_id}")],
                [InlineKeyboardButton("🔁 Перенос", callback_data=f"staff:transfer:{case_id}")],
                [InlineKeyboardButton("💸 Возврат", callback_data=f"staff:refund:{case_id}")],
            ]
        )

    async def staff_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if self.operator_id and query.from_user.id != self.operator_id:
            await query.answer("Недостаточно прав", show_alert=True)
            return

        _, action, case_id = query.data.split(":", 2)
        case = self.cases.get(case_id)
        if not case:
            await query.edit_message_text("Кейс не найден или уже архивирован")
            return

        status_map = {
            "close": "#done",
            "need_data": "#waiting_user",
            "take_ivan": "#escalated",
            "transfer": "#in_progress",
            "refund": "#in_progress",
        }
        notify_map = {
            "close": "Ваше обращение закрыто ✅",
            "need_data": "Нужны дополнительные данные. Ответьте в этом чате, и я передам в кейс.",
            "take_ivan": "Кейс передан Ивану (@i_abramov_gpt).",
            "transfer": "Запрос на перенос взят в работу.",
            "refund": "Запрос на возврат взят в работу.",
        }

        case.status = status_map[action]
        case.last_update = datetime.now(timezone.utc)
        await query.edit_message_text(
            text=self._render_case_card(case),
            reply_markup=self._staff_keyboard(case.case_id),
        )
        await self.app.bot.send_message(chat_id=case.user_id, text=notify_map[action])

    async def _sla_watchdog(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(timezone.utc)
        stale = [
            c
            for c in self.cases.values()
            if c.status in {"#new", "#in_progress"}
            and now - c.last_update > timedelta(minutes=self.sla_minutes)
        ]
        for case in stale:
            case.status = "#escalated"
            case.last_update = now
            if case.support_message_id:
                try:
                    await self.app.bot.edit_message_text(
                        chat_id=self.support_chat_id,
                        message_id=case.support_message_id,
                        text=self._render_case_card(case),
                        reply_markup=self._staff_keyboard(case.case_id),
                    )
                except Exception as exc:
                    logger.warning("Не удалось обновить кейс %s: %s", case.case_id, exc)
            await self.app.bot.send_message(
                chat_id=self.support_chat_id,
                text=f"⏰ SLA: кейс {case.case_id} просрочен, эскалирован Ивану.",
            )

    @staticmethod
    def _main_menu() -> ReplyKeyboardMarkup:
        keyboard = [[KeyboardButton(x)] for x in SUPPORT_MENU]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def run(self) -> None:
        self.app.run_polling(close_loop=False)


if __name__ == "__main__":
    SupportBot().run()
