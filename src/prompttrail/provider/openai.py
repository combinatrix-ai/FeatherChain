import os
import logging
import typing
from typing import Dict, Generator, List, Literal, Optional, Tuple

import openai
from pydantic import ConfigDict

from prompttrail.agent.tool import Tool
from prompttrail.const import CONTROL_TEMPLATE_ROLE
from prompttrail.core import Configuration, Message, Model, Parameters, Session
from prompttrail.error import ParameterValidationError
from prompttrail.mock import MockModel, MockProvider

logger = logging.getLogger(__name__)


class OpenAIModelConfiguration(Configuration):
    api_key: str
    organization_id: Optional[str] = None
    api_base: Optional[str] = None
    api_version: Optional[str] = None


class OpenAIModelParameters(Parameters):
    model_name: str
    temperature: Optional[float] = 0
    max_tokens: int = 1024
    functions: Optional[Dict[str, Tool]] = None

    # pydantic
    model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())


class OpenAIChatCompletionModel(Model):
    configuration: OpenAIModelConfiguration

    def _authenticate(self) -> None:
        openai.api_key = self.configuration.api_key  # type: ignore
        openai.organization = self.configuration.organization_id  # type: ignore
        if self.configuration.api_base is not None:
            openai.api_base = self.configuration.api_base  # type: ignore
        if self.configuration.api_version is not None:
            openai.api_version = self.configuration.api_version  # type: ignore

    def before_send(
        self, parameters: Parameters, session: Optional[Session], is_async: bool
    ) -> Tuple[Optional[Configuration], Optional[Parameters], Optional[Session]]:
        self._authenticate()
        return (None, None, None)

    def _send(self, parameters: Parameters, session: Session) -> Message:
        if not isinstance(parameters, OpenAIModelParameters):
            raise ParameterValidationError(
                f"{OpenAIModelParameters.__name__} is expected, but {type(parameters).__name__} is given."
            )
        # TODO: Add retry logic for http error and max_tokens_exceeded
        if parameters.functions is None:
            response = openai.ChatCompletion.create(  # type: ignore
                model=parameters.model_name,
                temperature=parameters.temperature,
                max_tokens=parameters.max_tokens,
                messages=self._session_to_openai_messages(session),
            )
        else:
            # TODO: Somwhow, function argument cannnot be passed with [] or None if you want not to invoke the function.
            response = openai.ChatCompletion.create(  # type: ignore
                model=parameters.model_name,
                temperature=parameters.temperature,
                max_tokens=parameters.max_tokens,
                messages=self._session_to_openai_messages(session),
                functions=[val.show() for _, val in parameters.functions.items()],
            )
        message = response.choices[0]["message"]  # type: ignore #TODO: More robust error handling
        content = message["content"]  # type: ignore
        if content is None:
            # This implies that the model responded with function calling
            content = ""  # TODO: Should allow null message? (It may be more clear that non textual response is returned)
        result = Message(content=content, sender=message["role"])  # type: ignore #TODO: More robust error handling
        # TODO: handle for _send_async
        if message.get("function_call"):  # type: ignore
            function_name = message["function_call"]["name"]  # type: ignore
            arguments = json.loads(message["function_call"]["arguments"])  # type: ignore
            result.data["function_call"] = {
                "name": function_name,
                "arguments": arguments,
            }
        return result

    def _send_async(
        self,
        parameters: Parameters,
        session: Session,
        yiled_type: Literal["all", "new"] = "new",
    ) -> Generator[Message, None, None]:
        if not isinstance(parameters, OpenAIModelParameters):
            raise ParameterValidationError(
                f"{OpenAIModelParameters.__name__} is expected, but {type(parameters).__name__} is given."
            )
        response: Generator[Dict[str, str], None, None] = openai.ChatCompletion.create(  # type: ignore
            model=parameters.model_name,
            temperature=parameters.temperature,
            max_tokens=parameters.max_tokens,
            messages=self._session_to_openai_messages(session),
            stream=True,
        )
        # response is a generator, and we want response[i]['choices'][0]['delta'].get('content', '')
        all_text: str = ""
        for message in response:
            new_text: str = message.choices[0]["delta"].get("content", "")  # type: ignore #TODO: More robust error handling
            if yiled_type == "new":
                yield Message(content=new_text, sender=None)  # type: ignore
            elif yiled_type == "all":
                all_text: str = all_text + new_text  # type: ignore
                yield Message(content=all_text, sender=None)  # type: ignore
            else:
                raise ParameterValidationError(
                    f"{self.__class__.__name__}: yiled_type should be 'all' or 'new'."
                )

    def validate_session(self, session: Session, is_async: bool) -> None:
        if len(session.messages) == 0:
            raise ParameterValidationError(
                f"{self.__class__.__name__}: Session should be a Session object and have at least one message."
            )
        if any([not isinstance(message.content, str) for message in session.messages]):  # type: ignore
            raise ParameterValidationError(
                f"{self.__class__.__name__}: All message in a session should be string."
            )
        if any([message.sender is None for message in session.messages]):
            raise ParameterValidationError(
                f"{self.__class__.__name__}: All message in a session should have sender."
            )
        allowed_senders = list(typing.get_args(OpenAIrole)) + [CONTROL_TEMPLATE_ROLE]
        if any(
            [
                message.sender not in allowed_senders
                # TODO: decide what to do with MetaTemplate (role=prompttrail)
                for message in session.messages
            ]
        ):
            raise ParameterValidationError(
                f"{self.__class__.__name__}: Sender should be one of {allowed_senders} in a session."
            )

    @staticmethod
    def _session_to_openai_messages(session: Session) -> List[Dict[str, str]]:
        # TODO: decide what to do with MetaTemplate (role=prompttrail)
        messages = [
            message
            for message in session.messages
            if message.sender != CONTROL_TEMPLATE_ROLE
        ]
        return [
            {
                "content": message.content,
                "role": message.sender,  # type: ignore
            }
            if "function_call" not in message.data
            # In this mode, we send the function name and content is the result of the function.
            else {
                "content": message.content,
                "role": message.sender,  # type: ignore
                "name": message.data["function_call"]["name"],
            }  # type: ignore
            for message in messages
        ]

    def list_models(self) -> List[str]:
        self._authenticate()
        response = openai.Model.list()  # type: ignore
        return [model.id for model in response.data]  # type: ignore


class OpenAIChatCompletionModelMock(OpenAIChatCompletionModel, MockModel):
    # TODO: This should be integrated to OpenAIChatCompletionModel itself?
    def setup(self, mock_provider: MockProvider) -> None:
        self.mock_provider: MockProvider = mock_provider

    def _send(self, parameters: Parameters, session: Session) -> Message:
        if not isinstance(parameters, OpenAIModelParameters):
            raise ParameterValidationError(
                f"{OpenAIModelParameters.__name__} is expected, but {type(parameters).__name__} is given."
            )
        return self.mock_provider.call(session)

    def _send_async(
        self,
        parameters: Parameters,
        session: Session,
        yiled_type: Literal["all", "new"] = "new",
    ) -> Generator[Message, None, None]:
        if not isinstance(parameters, OpenAIModelParameters):
            raise ParameterValidationError(
                f"{OpenAIModelParameters.__name__} is expected, but {type(parameters).__name__} is given."
            )
        message = self.mock_provider.call(session)
        if yiled_type == "new":
            for ch in message.content:
                yield Message(content=ch, sender=message.sender)
        elif yiled_type == "all":
            for idx in range(len(message.content)):
                yield Message(
                    content=message.content[: (idx + 1)], sender=message.sender
                )


OpenAIrole = Literal["system", "assistant", "user", "function"]
