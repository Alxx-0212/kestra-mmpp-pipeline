import unittest
from unittest.mock import AsyncMock, MagicMock
from telegram import InputFile

from services.telegram_bot.config import Settings
from services.telegram_bot.app import configure_command_menu, configure_webhook
from services.telegram_bot.callbacks import BOT_COMMANDS
from services.telegram_bot.service import ClassificationService


class TelegramAsyncCallsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = ClassificationService(Settings(bot_token="test", webhook_secret="test"))
        self.service.bot = MagicMock()
        self.service.bot.send_message = AsyncMock()
        self.service.bot.send_document = AsyncMock()

    async def test_send_awaits_ptb_coroutine(self):
        await self.service._send(123, "hello")
        self.service.bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="hello",
            reply_markup=None,
        )

    async def test_reply_and_edit_await_ptb_coroutines(self):
        message = MagicMock()
        message.reply_text = AsyncMock()
        await self.service._reply(message, "reply")
        message.reply_text.assert_awaited_once_with("reply")

        query = MagicMock()
        query.message.edit_text = AsyncMock()
        await self.service._edit(query, "edited")
        query.message.edit_text.assert_awaited_once_with("edited", reply_markup=None)

        query.message.edit_text.reset_mock()
        await self.service._edit(query, "<b>detail</b>", parse_mode="HTML")
        query.message.edit_text.assert_awaited_once_with(
            "<b>detail</b>", reply_markup=None, parse_mode="HTML"
        )

    async def test_document_delivery_uses_named_input_file(self):
        await self.service._send_document(123, b"xlsx", "report.xlsx", "caption")

        call = self.service.bot.send_document.await_args.kwargs
        self.assertEqual(call["chat_id"], 123)
        self.assertEqual(call["caption"], "caption")
        self.assertIsInstance(call["document"], InputFile)
        self.assertEqual(call["document"].filename, "report.xlsx")


class WebhookRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_command_menu_is_scoped_to_allowed_chats(self):
        bot = MagicMock()
        bot.set_my_commands = AsyncMock()
        bot.set_chat_menu_button = AsyncMock()
        settings = Settings(
            bot_token="test",
            webhook_secret="secret",
            allowed_chat_ids=frozenset({101, 202}),
        )

        await configure_command_menu(bot, settings)

        self.assertEqual(bot.set_my_commands.await_count, 2)
        self.assertEqual(bot.set_chat_menu_button.await_count, 2)
        self.assertEqual(
            [call.kwargs["scope"].chat_id for call in bot.set_my_commands.await_args_list],
            [101, 202],
        )
        registered = bot.set_my_commands.await_args_list[0].args[0]
        self.assertEqual(
            [(command.command, command.description) for command in registered],
            list(BOT_COMMANDS),
        )
        self.assertTrue(
            all(call.kwargs["menu_button"].type == "commands"
                for call in bot.set_chat_menu_button.await_args_list)
        )

    async def test_empty_public_url_keeps_manual_webhook_mode(self):
        bot = MagicMock()
        bot.set_webhook = AsyncMock()
        result = await configure_webhook(
            bot,
            Settings(bot_token="test", webhook_secret="secret"),
        )
        self.assertIsNone(result)
        bot.set_webhook.assert_not_called()

    async def test_https_public_url_registers_secret_webhook(self):
        bot = MagicMock()
        bot.set_webhook = AsyncMock(return_value=True)
        result = await configure_webhook(
            bot,
            Settings(
                bot_token="test",
                webhook_secret="secret",
                public_base_url="https://bot.example.com",
            ),
        )
        self.assertEqual(result, "https://bot.example.com/telegram/webhook")
        bot.set_webhook.assert_awaited_once_with(
            url="https://bot.example.com/telegram/webhook",
            secret_token="secret",
            allowed_updates=["message", "callback_query"],
        )

    async def test_non_https_public_url_fails_startup(self):
        with self.assertRaisesRegex(RuntimeError, "must use HTTPS"):
            await configure_webhook(
                MagicMock(),
                Settings(
                    bot_token="test",
                    webhook_secret="secret",
                    public_base_url="http://bot.example.com",
                ),
            )


if __name__ == "__main__":
    unittest.main()
