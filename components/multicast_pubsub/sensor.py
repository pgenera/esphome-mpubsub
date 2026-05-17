"""Sensor platform for multicast_pubsub.

YAML:
    sensor:
      - platform: multicast_pubsub
        topic: "home/livingroom/temp"
        name: "Living Room Temperature"
        mode: subscribe          # subscribe | publish | both (default: subscribe)
        # standard sensor.sensor_schema knobs (unit, accuracy_decimals, ...)
        # apply as usual.

For subscribers, incoming payloads are parsed as an ASCII float and the
sensor's state is updated. For publishers, the sensor's `add_on_state_callback`
is hooked and each state change is sent as `"%.6f"` (configurable via
``accuracy_decimals``) to the topic.
"""

import esphome.codegen as cg
from esphome.components import sensor
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MODE

from . import (
    CONF_TOPIC,
    MulticastPubSub,
    _topic_validator,
    multicast_pubsub_ns,
)

DEPENDENCIES = ["multicast_pubsub"]

CONF_PARENT_ID = "multicast_pubsub_id"

MODE_SUBSCRIBE = "subscribe"
MODE_PUBLISH = "publish"
MODE_BOTH = "both"
MODES = [MODE_SUBSCRIBE, MODE_PUBLISH, MODE_BOTH]

MulticastSensor = multicast_pubsub_ns.class_(
    "MulticastSensor", sensor.Sensor, cg.Component
)

CONFIG_SCHEMA = (
    sensor.sensor_schema(MulticastSensor)
    .extend(
        {
            cv.GenerateID(CONF_PARENT_ID): cv.use_id(MulticastPubSub),
            cv.Required(CONF_TOPIC): _topic_validator,
            cv.Optional(CONF_MODE, default=MODE_SUBSCRIBE): cv.one_of(*MODES, lower=True),
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = await sensor.new_sensor(config)
    await cg.register_component(var, config)
    parent = await cg.get_variable(config[CONF_PARENT_ID])
    cg.add(var.set_parent(parent))
    cg.add(var.set_topic(config[CONF_TOPIC]))
    mode = config[CONF_MODE]
    cg.add(var.set_subscribe(mode in (MODE_SUBSCRIBE, MODE_BOTH)))
    cg.add(var.set_publish(mode in (MODE_PUBLISH, MODE_BOTH)))
