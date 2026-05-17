"""ESPHome external component: brokerless IPv6 multicast publish/subscribe.

See ../../README.md for the protocol specification.
"""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    CONF_PAYLOAD,
    CONF_PORT,
    CONF_TOPIC,
    CONF_TRIGGER_ID,
)

CODEOWNERS = ["@pgenera"]
DEPENDENCIES = ["network"]
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

SCOPES = {
    "link-local": Scope.LINK_LOCAL,
    "site-local": Scope.SITE_LOCAL,
    "organization-local": Scope.ORG_LOCAL,
}

# Mirrors components/multicast_pubsub/wire_format.h. Hard cap.
MAX_DATAGRAM = 508
HEADER_LEN = 12
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_LEN  # 496


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


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(MulticastPubSub),
        cv.Optional(CONF_PORT, default=18512): cv.port,
        cv.Optional(CONF_SCOPE, default="link-local"): cv.enum(SCOPES, lower=True),
        cv.Optional(CONF_HOPS, default=1): cv.int_range(min=1, max=255),
        cv.Optional(CONF_ON_MESSAGE): automation.validate_automation(
            {
                cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(OnMessageTrigger),
                cv.Required(CONF_TOPIC): _topic_validator,
            },
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_scope(config[CONF_SCOPE]))
    cg.add(var.set_hops(config[CONF_HOPS]))

    for conf in config.get(CONF_ON_MESSAGE, []):
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
