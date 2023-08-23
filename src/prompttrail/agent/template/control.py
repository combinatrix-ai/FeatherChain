import logging
from abc import abstractmethod
from typing import Generator, List, Optional, Sequence, Set, TypeAlias

from prompttrail.agent.core import State
from prompttrail.agent.hook.core import BooleanHook, TransformHook
from prompttrail.agent.template.core import Stack, Template
from prompttrail.const import (
    END_TEMPLATE_ID,
    BreakException,
    JumpException,
    ReachedEndTemplateException,
)
from prompttrail.core import Message

logger = logging.getLogger(__name__)

TemplateId: TypeAlias = str


class ControlTemplate(Template):
    """
    Base class for control flow templates.
    """

    @abstractmethod
    def __init__(
        self,
        template_id: Optional[str] = None,
        before_transform: Optional[List[TransformHook]] = None,
        after_transform: Optional[List[TransformHook]] = None,
    ):
        super().__init__(
            template_id=template_id,
            before_transform=before_transform if before_transform is not None else [],
            after_transform=after_transform if after_transform is not None else [],
        )

    @abstractmethod
    def walk(
        self, visited_templates: Optional[Set["Template"]] = None
    ) -> Generator["Template", None, None]:
        """
        Traverse the template and its child templates in a depth-first manner.

        Args:
            visited_templates: Set of visited templates to avoid infinite recursion.

        Yields:
            The template and its child templates.
        """
        raise NotImplementedError(
            "Derived class of ControlTemplate must implement its own walk method"
        )

    @abstractmethod
    def create_stack(self, state: "State") -> "Stack":
        """
        Create a stack for the control template.

        Args:
            state: The current state of the conversation.

        Returns:
            The created stack.
        """
        raise NotImplementedError(
            "Derived class of ControlTemplate must implement its own create_stack method"
        )


class LoopTemplateStack(Stack):
    """
    Stack for LoopTemplate.
    """

    cumulative_index: int
    n_children: int

    def get_loop_idx(self) -> int:
        """
        Get the index of the current loop.

        Returns:
            The index of the current loop.
        """
        return self.cumulative_index // self.n_children

    def get_idx(self) -> int:
        """
        Get the index of the current child template.

        Returns:
            The index of the current child template.
        """
        return self.cumulative_index % self.n_children

    def next(self) -> None:
        """
        Move to the next child template.
        """
        self.cumulative_index += 1


class LoopTemplate(ControlTemplate):
    """
    Template for a loop control flow.
    """

    def __init__(
        self,
        templates: Sequence[Template],
        exit_condition: Optional[BooleanHook] = None,
        template_id: Optional[str] = None,
        exit_loop_count: Optional[int] = None,
        before_transform: Optional[List[TransformHook]] = None,
        after_transform: Optional[List[TransformHook]] = None,
    ):
        super().__init__(
            template_id=template_id,
            before_transform=before_transform if before_transform is not None else [],
            after_transform=after_transform if after_transform is not None else [],
        )
        self.templates = templates
        self.exit_condition = exit_condition
        self.exit_loop_count = exit_loop_count

    def _render(self, state: "State") -> Generator[Message, None, State]:
        stack = state.stack[-1]
        if not isinstance(stack, LoopTemplateStack):
            raise RuntimeError("LoopTemplateStack is not the last stack")
        while True:
            idx = stack.get_idx()
            template = self.templates[idx]
            gen = template.render(state)
            try:
                state = yield from gen
            except BreakException:
                # Break the loop if a BreakException is raised.
                break
            if self.exit_condition and self.exit_condition.hook(state):
                # Break the loop if the exit condition is met.
                break
            stack.next()
            if (
                self.exit_loop_count is not None
                and stack.get_loop_idx() >= self.exit_loop_count
            ):
                logger.warning(
                    msg=f"Loop count is over {self.exit_loop_count}. Breaking the loop."
                )
                break
        return state

    def walk(
        self, visited_templates: Optional[Set["Template"]] = None
    ) -> Generator["Template", None, None]:
        visited_templates = visited_templates or set()
        if self in visited_templates:
            return
        visited_templates.add(self)
        yield self
        for template in self.templates:
            yield from template.walk(visited_templates)

    def create_stack(self, state: "State") -> "Stack":
        return LoopTemplateStack(
            template_id=self.template_id,
            n_children=len(self.templates),
            cumulative_index=0,
        )


class IfTemplate(ControlTemplate):
    """
    Template for an if-else control flow.
    """

    def __init__(
        self,
        condition: BooleanHook,
        true_template: Template,
        false_template: Optional[Template] = None,
        template_id: Optional[str] = None,
        before_transform: Optional[List[TransformHook]] = None,
        after_transform: Optional[List[TransformHook]] = None,
    ):
        super().__init__(
            template_id=template_id,
            before_transform=before_transform if before_transform is not None else [],
            after_transform=after_transform if after_transform is not None else [],
        )
        self.true_template = true_template
        self.false_template = false_template
        self.condition = condition

    def _render(self, state: "State") -> Generator[Message, None, State]:
        if self.condition.hook(state):
            state = yield from self.true_template.render(state)
        else:
            if self.false_template is None:
                pass  # Just skip to the next template
            else:
                state = yield from self.false_template.render(state)
        return state

    def walk(
        self, visited_templates: Optional[Set["Template"]] = None
    ) -> Generator["Template", None, None]:
        visited_templates = visited_templates or set()
        if self in visited_templates:
            return
        visited_templates.add(self)
        yield self
        yield from self.true_template.walk(visited_templates)
        if self.false_template is not None:
            yield from self.false_template.walk(visited_templates)

    def create_stack(self, state: "State") -> "Stack":
        return Stack(template_id=self.template_id)


class LinearTemplateStack(Stack):
    """
    Stack for LinearTemplate.
    """

    idx: int


class LinearTemplate(ControlTemplate):
    """
    Template for a linear control flow.
    """

    def __init__(
        self,
        templates: Sequence[Template],
        template_id: Optional[str] = None,
        before_transform: Optional[List[TransformHook]] = None,
        after_transform: Optional[List[TransformHook]] = None,
    ):
        super().__init__(
            template_id=template_id,
            before_transform=before_transform if before_transform is not None else [],
            after_transform=after_transform if after_transform is not None else [],
        )
        self.templates = templates

    def _render(self, state: "State") -> Generator[Message, None, State]:
        stack = state.stack[-1]
        if not isinstance(stack, LinearTemplateStack):
            raise RuntimeError("LinearTemplateStack is not the last stack")

        while 1:
            try:
                state = yield from self.templates[stack.idx].render(state)
            except BreakException:
                break
            stack.idx += 1
            # Break the loop when the last template is rendered
            if stack.idx >= len(self.templates):
                break
        return state

    def walk(
        self, visited_templates: Optional[Set["Template"]] = None
    ) -> Generator["Template", None, None]:
        visited_templates = visited_templates or set()
        if self in visited_templates:
            return
        visited_templates.add(self)
        yield self
        for template in self.templates:
            yield from template.walk(visited_templates)

    def create_stack(self, state: "State") -> LinearTemplateStack:
        return LinearTemplateStack(template_id=self.template_id, idx=0)


# EndTemplate is a singleton
class EndTemplate(Template):
    """
    Template for the end of the conversation.
    """

    template_id = END_TEMPLATE_ID
    _instance = None
    before_transform = []

    def __init__(self):
        pass  # No configuration is needed here.

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EndTemplate, cls).__new__(cls)
        return cls._instance

    def _render(self, state: "State") -> Generator[Message, None, State]:
        raise ReachedEndTemplateException()

    def create_stack(self, state: State) -> Stack:
        return super().create_stack(state)


class JumpTemplate(ControlTemplate):
    """
    Template for jumping to another template.
    """

    def __init__(
        self,
        jump_to: Template | TemplateId,
        condition: BooleanHook,
        template_id: Optional[str] = None,
        before_transform: Optional[List[TransformHook]] = None,
    ):
        super().__init__(
            template_id=template_id,
            before_transform=before_transform if before_transform is not None else [],
            # after_transform is unavailable for the templates that raise errors
        )
        if isinstance(jump_to, Template):
            jump_to = jump_to.template_id
        self.jump_to = jump_to
        self.condition = condition

    def _render(self, state: "State") -> Generator[Message, None, State]:
        logger.warning(
            msg=f"Jumping to {self.jump_to} from {self.template_id}. This resets the stack, and the dialogue will not come back to this template."
        )
        raise JumpException(self.jump_to)

    def walk(
        self, visited_templates: Optional[Set["Template"]] = None
    ) -> Generator["Template", None, None]:
        visited_templates = visited_templates or set()
        if self in visited_templates:
            return
        visited_templates.add(self)
        yield self

    def create_stack(self, state: "State") -> LinearTemplateStack:
        return LinearTemplateStack(template_id=self.template_id, idx=0)


class BreakTemplate(ControlTemplate):
    """
    Template for breaking out of a loop.
    """

    def __init__(
        self,
        template_id: Optional[str] = None,
        before_transform: Optional[List[TransformHook]] = None,
    ):
        super().__init__(
            template_id=template_id,
            before_transform=before_transform if before_transform is not None else [],
            # after_transform is unavailable for the templates that raise errors
        )

    def _render(self, state: "State") -> Generator[Message, None, State]:
        logger.info(msg=f"Breaking the loop from {self.template_id}.")
        raise BreakException()

    def walk(
        self, visited_templates: Optional[Set["Template"]] = None
    ) -> Generator["Template", None, None]:
        visited_templates = visited_templates or set()
        yield self

    def create_stack(self, state: "State") -> Stack:
        return Stack(template_id=self.template_id)
