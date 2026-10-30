"""Bind the arguments the callable will actually receive before inspection."""
import copy
import inspect


def bind_tool_call(fn, args, kwargs):
    if inspect.iscoroutinefunction(fn):
        raise TypeError("Async tools require an async adapter")
    signature = inspect.signature(fn)
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    bound.arguments = copy.deepcopy(bound.arguments)
    payload = {}
    for name, value in bound.arguments.items():
        if signature.parameters[name].kind == inspect.Parameter.VAR_KEYWORD:
            payload.update(value)
        else:
            payload[name] = value
    return payload, bound
