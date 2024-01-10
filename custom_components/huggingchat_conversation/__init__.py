"""The HuggingChat Conversation integration."""
from __future__ import annotations

import logging
from typing import Literal

from hugchat import hugchat
from hugchat.login import Login

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import intent, template

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    DEFAULT_CHAT_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HuggingChat Conversation from a config entry."""
    conversation.async_set_agent(hass, entry, HuggingChatAgent(hass, entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload HuggingChat."""
    conversation.async_unset_agent(hass, entry)
    return True


class HuggingChatAgent(conversation.AbstractConversationAgent):
    """HuggingChat conversation agent."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry

        self.history: dict[str, list[dict]] = {}

    @property
    def attribution(self):
        """Return the attribution."""
        return {"name": "Powered by HuggingChat", "url": "https://github.com/PhoenixR49/hass-huggingchat-conversation"}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""
        email = self.entry.data[CONF_EMAIL]
        passwd = self.entry.data[CONF_PASSWORD]
        raw_prompt = self.entry.options.get(CONF_PROMPT, DEFAULT_PROMPT)
        model = int(self.entry.options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL))
        temperature = self.entry.options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)
        top_p = self.entry.options.get(CONF_TOP_P, DEFAULT_TOP_P)
        max_tokens = self.entry.options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)

        cookie_path_dir = "./config/custom_components/huggingchat_conversation/cookies_snapshot"

        # Log in to huggingface and grant authorization to huggingchat
        sign = Login(email, passwd)

        try:
            cookies = sign.loadCookiesFromDir(cookie_path_dir)
        except Exception:
            cookies = await self.hass.async_add_executor_job(sign.login)
            sign.saveCookiesToDir(cookie_path_dir)

        def initialize_chatbot(cookies, model, prompt):
            return hugchat.ChatBot(cookies=cookies, default_llm=model, system_prompt=prompt)

        try:
            chatbot = await self.hass.async_add_executor_job(
                initialize_chatbot, cookies.get_dict(), model, ""
            )
        except hugchat.exceptions.ChatBotInitError as err:
            _LOGGER.error("Chat initialisation error: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Sorry, an error occurred when init*ialising the chat: {err}"
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=user_input.conversation_id
            )

        if user_input.conversation_id in self.history:
            conversation_id = user_input.conversation_id
            messages = self.history[conversation_id]

            await self.hass.async_add_executor_job(chatbot.get_remote_conversations, True)
            conversation_object = chatbot.get_conversation_from_id(conversation_id)

            chatbot.change_conversation(conversation_object)
        else:
            # Set conversation_id to the HuggingChat conversation ID
            info = await self.hass.async_add_executor_job(chatbot.get_conversation_info)
            conversation_id = info.id

            try:
                prompt = self._async_generate_prompt(raw_prompt)
            except TemplateError as err:
                _LOGGER.error("Error rendering prompt: %s", err)
                intent_response = intent.IntentResponse(language=user_input.language)
                intent_response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    f"Sorry, I had a problem with my template: {err}",
                )
                return conversation.ConversationResult(
                    response=intent_response, conversation_id=conversation_id
                )

            chatbot = await self.hass.async_add_executor_job(
                initialize_chatbot, cookies.get_dict(), model, prompt,
            )
            await self.hass.async_add_executor_job(chatbot.get_remote_conversations, True)
            chatbot.change_conversation(info)

            messages = [{"role": "system", "content": prompt}]

        messages.append({"role": "user", "content": user_input.text})

        _LOGGER.debug("Prompt for %s: %s", model, messages)

        try:
            result = await self.hass.async_add_executor_job(str, chatbot.query(text=user_input.text, temperature=temperature, top_p=top_p, max_new_tokens=max_tokens))
        except hugchat.exceptions.ChatError as err:
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Sorry, I had a problem talking to HuggingChat server: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )
        except hugchat.exceptions.ModelOverloadedError as err:
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Sorry, the HuggingChat model is overloaded: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        _LOGGER.debug("Response %s", result)
        messages.append({"role": "assistant", "content": result})
        self.history[conversation_id] = messages

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(result)
        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    def _async_generate_prompt(self, raw_prompt: str) -> str:
        """Generate a prompt for the user."""
        return template.Template(raw_prompt, self.hass).async_render(
            {
                "ha_name": self.hass.config.location_name,
            },
            parse_result=False,
        )