"""ESPHome external component: brokerless IPv6 multicast publish/subscribe.

See ../../README.md for the protocol specification.
"""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    CONF_NAME,
    CONF_PAYLOAD,
    CONF_PORT,
    CONF_TOPIC,
    CONF_TRIGGER_ID,
    CONF_TYPE,
)

from esphome import final_validate as fv

from . import proto_emitter

CODEOWNERS = ["@pgenera"]
DEPENDENCIES = ["network"]
# We AUTO_LOAD `api` only when the user actually declares typed messages:
# in to_code below. Devices using only raw publish/subscribe don't pay the
# ~10KB cost of the API server. (A future upstream refactor splitting
# api/proto.{h,cpp} into a leaf `api_proto` sub-component would let us
# bring in only the protobuf primitives without the server runtime.)
AUTO_LOAD = ["socket"]
MULTI_CONF = True

multicast_pubsub_ns = cg.esphome_ns.namespace("multicast_pubsub")
MulticastPubSub = multicast_pubsub_ns.class_("MulticastPubSub", cg.Component)
Scope = multicast_pubsub_ns.enum("Scope", is_class=True)
PublishAction = multicast_pubsub_ns.class_("PublishAction", automation.Action)
OnMessageTrigger = multicast_pubsub_ns.class_("OnMessageTrigger", automation.Trigger)

CONF_SCOPE = "scope"
CONF_HOPS = "hops"
CONF_ON_MESSAGE = "on_message"
CONF_MESSAGES = "messages"
CONF_FIELDS = "fields"
CONF_TAG = "tag"
CONF_MESSAGE = "message"
CONF_REPEATED = "repeated"

SCOPES = {
    "link-local": Scope.LINK_LOCAL,
    "site-local": Scope.SITE_LOCAL,
    "organization-local": Scope.ORG_LOCAL,
}

# Mirrors components/multicast_pubsub/wire_format.h. Hard cap.
# IPv6 minimum MTU (1280, RFC 8200 §5) minus IPv6 header (40) minus UDP
# header (8) = 1232 bytes deliverable on any IPv6 link without fragmentation.
MAX_DATAGRAM = 1232
HEADER_LEN = 12
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_LEN  # 1220


def _static_payload_validator(value):
    """Reject configs whose payload is a static string > MAX_PAYLOAD.

    Templatable payloads (lambdas) are validated at runtime by the C++ side.
    """
    value = cv.string(value)
    encoded_len = len(value.encode("utf-8"))
    if encoded_len > MAX_PAYLOAD:
        raise cv.Invalid(
            f"payload is {encoded_len} bytes but the maximum publishable size "
            f"is {MAX_PAYLOAD} bytes (UDP datagram limit {MAX_DATAGRAM} minus "
            f"the {HEADER_LEN}-byte header)"
        )
    return value


def _message_id_validator(value):
    """Validate a typed-message id (used to reference the message from publish
    actions and on_message triggers).

    Restrict to identifier-like strings so the generated C++ type name is
    sane: alphanumeric + underscore/hyphen, starts with a letter.
    """
    value = cv.string_strict(value)
    if not value:
        raise cv.Invalid("message id must be non-empty")
    if not (value[0].isalpha() or value[0] == "_"):
        raise cv.Invalid(f"message id {value!r} must start with a letter or underscore")
    for c in value:
        if not (c.isalnum() or c in "_-"):
            raise cv.Invalid(
                f"message id {value!r}: invalid character {c!r} (allowed: alphanumeric, _, -)"
            )
    return value


def _field_name_validator(value):
    value = cv.string_strict(value)
    if not value:
        raise cv.Invalid("field name must be non-empty")
    if not (value[0].isalpha() or value[0] == "_"):
        raise cv.Invalid(f"field name {value!r} must start with a letter or underscore")
    for c in value:
        if not (c.isalnum() or c == "_"):
            raise cv.Invalid(
                f"field name {value!r}: invalid character {c!r} (allowed: alphanumeric, _)"
            )
    return value


def _field_type_validator(value):
    value = cv.string_strict(value)
    if value not in proto_emitter.VALID_TYPES:
        raise cv.Invalid(
            f"unknown field type {value!r}. Valid: {sorted(proto_emitter.VALID_TYPES)}"
        )
    return value


def _field_tag_validator(value):
    value = cv.positive_int(value)
    if not (1 <= value <= 536_870_911):
        raise cv.Invalid(f"field tag {value} out of range (1..536870911)")
    if 19000 <= value <= 19999:
        raise cv.Invalid(f"field tag {value} is in proto3's reserved range 19000..19999")
    return value


FIELD_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_NAME): _field_name_validator,
        cv.Required(CONF_TYPE): _field_type_validator,
        cv.Required(CONF_TAG): _field_tag_validator,
        cv.Optional(CONF_REPEATED, default=False): cv.boolean,
    }
)


def _message_validator(value):
    """Validate a single message declaration and raise the cv-friendly form
    of any proto_emitter errors."""
    schema = cv.Schema(
        {
            cv.Required(CONF_ID): _message_id_validator,
            cv.Required(CONF_FIELDS): cv.All(
                cv.ensure_list(FIELD_SCHEMA), cv.Length(min=1)
            ),
        }
    )
    value = schema(value)
    # Run the emitter's structural validation (duplicate tags etc) and
    # surface anything it complains about as a cv.Invalid.
    msg = proto_emitter.Message(
        id=value[CONF_ID],
        fields=tuple(
            proto_emitter.Field(
                name=f[CONF_NAME],
                type=f[CONF_TYPE],
                tag=f[CONF_TAG],
                repeated=f[CONF_REPEATED],
            )
            for f in value[CONF_FIELDS]
        ),
    )
    try:
        proto_emitter.validate(msg)
    except ValueError as e:
        raise cv.Invalid(str(e)) from e
    return value


def _messages_validator(value):
    """Validate the top-level messages: list and ensure ids are unique."""
    value = cv.ensure_list(_message_validator)(value)
    seen_ids = set()
    for msg in value:
        if msg[CONF_ID] in seen_ids:
            raise cv.Invalid(f"duplicate message id: {msg[CONF_ID]!r}")
        seen_ids.add(msg[CONF_ID])
    return value


def _topic_validator(value):
    """Validate a pub/sub topic.

    Topics are arbitrary non-empty UTF-8 strings with no NUL bytes. We do not
    enforce MQTT-style wildcards or hierarchy — that's an application choice.
    """
    value = cv.string_strict(value)
    if "\x00" in value:
        raise cv.Invalid("topic must not contain NUL bytes")
    encoded = value.encode("utf-8")
    if len(encoded) == 0:
        raise cv.Invalid("topic must not be empty")
    if len(encoded) > 200:
        raise cv.Invalid(f"topic too long: {len(encoded)} bytes (limit 200)")
    return value


def _on_message_validator(config):
    """Top-level wire-up: each on_message: entry may opt into a typed
    schema by setting ``message: <schema_id>``. The trigger class used at
    codegen time is then the generated ``On<Pascal>Trigger`` rather than
    the raw byte-vector ``OnMessageTrigger``.

    The actual codegen happens in ``to_code`` once we know which message
    schemas exist; the schema id reference is resolved there.
    """
    # Wrap the underlying automation validator without re-declaring its
    # trigger id (the dynamic-class case rewrites the id type below).
    return automation.validate_automation(
        {
            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(OnMessageTrigger),
            cv.Required(CONF_TOPIC): _topic_validator,
            cv.Optional(CONF_MESSAGE): _message_id_validator,
        }
    )(config)


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(MulticastPubSub),
        cv.Optional(CONF_PORT, default=18512): cv.port,
        cv.Optional(CONF_SCOPE, default="link-local"): cv.enum(SCOPES, lower=True),
        cv.Optional(CONF_HOPS, default=1): cv.int_range(min=1, max=255),
        cv.Optional(CONF_MESSAGES, default=list): _messages_validator,
        cv.Optional(CONF_ON_MESSAGE): _on_message_validator,
    }
).extend(cv.COMPONENT_SCHEMA)


def _final_validate(config):
    """Two checks that can only run after all schemas have evaluated:

    1. If `messages:` is declared, `api:` must be present (we depend on
       its protobuf primitives in components/api/proto.h).
    2. If any `on_message: + message:` references a schema id, that
       schema must actually be declared in `messages:`.
    """
    if config.get(CONF_MESSAGES):
        full = fv.full_config.get()
        if "api" not in full:
            raise cv.Invalid(
                "multicast_pubsub `messages:` requires the `api:` component to "
                "be configured (used for protobuf encoding primitives). Add an "
                "`api:` section to your YAML, or remove the `messages:` block "
                "to stick to raw payloads.",
                path=[CONF_MESSAGES],
            )

    declared = {m[CONF_ID] for m in config.get(CONF_MESSAGES, [])}
    for i, trig in enumerate(config.get(CONF_ON_MESSAGE, [])):
        ref = trig.get(CONF_MESSAGE)
        if ref is None:
            continue
        if ref not in declared:
            raise cv.Invalid(
                f"on_message references unknown message {ref!r}; declare it "
                f"under `messages:`. Known: {sorted(declared) or '(none)'}",
                path=[CONF_ON_MESSAGE, i, CONF_MESSAGE],
            )
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


async def to_code(config):
    # Typed messages reference esphome::api::ProtoEncode / ProtoDecodableMessage,
    # so pull in proto.h from the api component (presence enforced by
    # FINAL_VALIDATE_SCHEMA above). cg.add_global appends a ';' to whatever
    # we emit, which a preprocessor directive doesn't want; the safest trick
    # is to wrap it in a dummy statement that swallows the trailing token.
    if config.get(CONF_MESSAGES):
        cg.add_global(cg.RawExpression(
            '#include "esphome/components/api/proto.h"\n'
            'struct multicast_pubsub_force_proto_include_'
        ))
    for msg_cfg in config.get(CONF_MESSAGES, []):
        msg = proto_emitter.Message(
            id=msg_cfg[CONF_ID],
            fields=tuple(
                proto_emitter.Field(
                    name=f[CONF_NAME],
                    type=f[CONF_TYPE],
                    tag=f[CONF_TAG],
                    repeated=f[CONF_REPEATED],
                )
                for f in msg_cfg[CONF_FIELDS]
            ),
        )
        cg.add_global(cg.RawExpression(proto_emitter.emit_struct(msg)))

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_scope(config[CONF_SCOPE]))
    cg.add(var.set_hops(config[CONF_HOPS]))

    for conf in config.get(CONF_ON_MESSAGE, []):
        if CONF_MESSAGE in conf:
            # Typed receive: instantiate the generated On<Pascal>Trigger
            # class that subscribes via subscribe_typed<T> and emits the
            # decoded struct as the trigger argument.
            class_name = proto_emitter.pascal_case(conf[CONF_MESSAGE])
            trigger_cls = multicast_pubsub_ns.class_(
                f"On{class_name}Trigger", automation.Trigger
            )
            msg_struct = multicast_pubsub_ns.namespace("messages").class_(class_name)
            # Re-declare the trigger id with the typed class so cg.new_Pvariable
            # produces the right C++ type.
            trigger_id = conf[CONF_TRIGGER_ID]
            trigger_id.type = trigger_cls
            trigger = cg.new_Pvariable(trigger_id, var, conf[CONF_TOPIC])
            # Trigger<T> hands the user lambda `T x` by value, matching the
            # `Trigger<MsgStruct>` base of the generated trigger class.
            await automation.build_automation(trigger, [(msg_struct, "x")], conf)
        else:
            # Raw receive: bytes vector argument, original behavior.
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var, conf[CONF_TOPIC])
            await automation.build_automation(
                trigger, [(cg.std_vector.template(cg.uint8), "x")], conf
            )


PUBLISH_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(MulticastPubSub),
        cv.Required(CONF_TOPIC): cv.templatable(_topic_validator),
        cv.Required(CONF_PAYLOAD): cv.templatable(_static_payload_validator),
    }
)


@automation.register_action(
    "multicast_pubsub.publish",
    PublishAction,
    PUBLISH_ACTION_SCHEMA,
    synchronous=True,
)
async def publish_action_to_code(config, action_id, template_arg, args):
    parent = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_arg, parent)
    topic_tpl = await cg.templatable(config[CONF_TOPIC], args, cg.std_string)
    cg.add(var.set_topic(topic_tpl))
    payload_tpl = await cg.templatable(config[CONF_PAYLOAD], args, cg.std_string)
    cg.add(var.set_payload(payload_tpl))
    return var
