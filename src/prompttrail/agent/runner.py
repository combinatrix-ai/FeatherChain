import logging
from abc import abstractmethod
from typing import Dict, Optional, Sequence

from prompttrail.agent import FlowState
from prompttrail.agent.core import StatefulSession
from prompttrail.agent.template import (
    END_TEMPLATE_ID,
    MAX_TEMPLATE_LOOP,
    EndTemplate,
    Template,
    TemplateId,
)
from prompttrail.agent.user_interaction import UserInteractionProvider
from prompttrail.core import Model, Parameters

logger = logging.getLogger(__name__)


class Runner(object):
    def __init__(
        self,
        model: Model,
        parameters: Parameters,
        templates: Sequence["Template"],
        user_interaction_provider: UserInteractionProvider,
        flow_state: Optional[FlowState] = None,
    ):
        self.model = model
        self.parameters = parameters
        self.user_interaction_provider = user_interaction_provider
        self.templates = templates
        self.flow_state = flow_state
        self.template_dict: Dict[TemplateId, Template] = {}
        visited_templates: Sequence[Template] = []
        for template in templates:
            for next_template in template.walk(visited_templates):
                if next_template.template_id in self.template_dict:
                    raise ValueError(
                        f"Template id {next_template.template_id} is duplicated."
                    )
                self.template_dict[next_template.template_id] = next_template  # type: ignore

    @abstractmethod
    def run(
        self,
        start_template_id: Optional[TemplateId] = None,
        flow_state: Optional[FlowState] = None,
        max_messages: Optional[int] = None,
    ) -> FlowState:
        raise NotImplementedError("run method is not implemented")

    def search_template(self, template_like: TemplateId) -> "Template":
        if template_like == EndTemplate.template_id:
            return EndTemplate()
        if template_like not in self.template_dict:
            raise ValueError(f"Template id {template_like} is not found.")
        return self.template_dict[template_like]


class CommandLineRunner(Runner):
    def run(
        self,
        start_template_id: Optional[TemplateId] = None,
        flow_state: Optional[FlowState] = None,
        max_messages: Optional[int] = 100,
    ) -> FlowState:
        # set / update flow_state
        if flow_state is None:
            flow_state = FlowState(
                runner=self,
                model=self.model,
                parameters=self.parameters,
                data={},
                session_history=StatefulSession(),
                jump_to_id=None,
            )
        else:
            if flow_state.model != self.model:
                logger.log(
                    logging.INFO,
                    f"Given flow state has different model {flow_state.model} from the runner {self.model}. Overriding the flow state.",
                )
                flow_state.model = self.model
            if flow_state.parameters != self.parameters:
                logger.log(
                    logging.INFO,
                    f"Given flow state has different parameters {flow_state.parameters} from the runner {self.parameters}. Overriding the flow state.",
                )
                flow_state.parameters = self.parameters
            if flow_state.runner is None or flow_state.runner != self:
                logger.log(
                    logging.INFO,
                    f"Given flow state has different runner {flow_state.runner} from the runner {self}. Overriding the flow state.",
                )
                flow_state.runner = self

        # decide where to start running
        if start_template_id is not None:
            start_template_id = self.search_template(start_template_id).template_id

        # main loop
        current_template_id = (
            start_template_id
            if start_template_id is not None
            else self.templates[0].template_id
        )
        last_template_id = None
        same_template_count = 0
        next_message_index_to_show = 0
        next_template = None
        while 1:
            flow_state = self.search_template(current_template_id).render(flow_state)
            logger.error(flow_state)

            # show newly added messages
            new_messages = flow_state.session_history.messages[
                next_message_index_to_show:
            ]
            for message in new_messages:
                print(message)
            next_message_index_to_show = len(flow_state.session_history.messages)

            # calculate next template

            # Next template is resolved as follows:
            # Step 1. If there is a jump, jump to the next template.
            # Step 2. If there is no jump, and the current template has a default next template, go to the next template.
            # Step 3. If there is no jump, and the current template does not have a default next template, finish the flow.
            # In any case, "END" or EndTemplate is a special template that finishes the flow.
            # MetaTemplate handle the default next template for each child template.

            # Step 1: handle jump
            if flow_state.jump_to_id is not None:
                logger.info(msg=f"Jump is set to {flow_state.jump_to_id}.")
                next_template = self.search_template(flow_state.jump_to_id)
                flow_state.jump_to_id = None
            else:
                # TODO: Naming here is confusing. We should rename this.
                # Step 2: handle default next template
                if flow_state.current_template_id is None:
                    raise ValueError("current_template is not set.")
                current_template = self.search_template(
                    template_like=flow_state.current_template_id
                )
                next_template_id = current_template.next_template_default
                next_template = (
                    self.search_template(next_template_id)
                    if next_template_id is not None
                    else None
                )
            if next_template and next_template.template_id == END_TEMPLATE_ID:
                logger.info("Next template is END. Flow is finished.")
                break

            # Step 3: no jump and no next template
            if next_template is None:
                logger.info("No jump is set. Flow is finished.")
                break

            # check if the same template is rendered consecutively
            # TODO: Check more general loop
            if last_template_id == next_template.template_id:
                same_template_count += 1
                if same_template_count > MAX_TEMPLATE_LOOP:
                    raise RuntimeError(
                        f"Same template is rendered consecutively more than {MAX_TEMPLATE_LOOP} times. This may be caused by infinite loop?"
                    )
            last_template_id = current_template_id
            current_template_id = next_template.template_id
            next_template = None
            if (
                max_messages
                and len(flow_state.session_history.messages) >= max_messages
            ):
                logger.warning(
                    f"Max messages {max_messages} is reached. Flow is forced to stop."
                )
                break
        return flow_state
