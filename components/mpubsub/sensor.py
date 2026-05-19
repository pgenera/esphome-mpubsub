"""Sensor platform for mpubsub.

YAML:
    sensor:
      - platform: mpubsub
        topic: "home/livingroom/temp"
        name: "Living Room Temperature"
        mode: subscribe          # subscribe (default) | publish
        # standard sensor.sensor_schema knobs (unit, accuracy_decimals, ...)
        # apply as usual.

For subscribers, incoming payloads are parsed as an ASCII float and the
sensor's state is updated. For publishers, the sensor's `add_on_state_callback`
is hooked and each state change is sent as ASCII to the topic.

A `both` mode used to be supported but was removed: combining publish
and subscribe on the *same topic* is semantically incoherent (a sensor
is a single state slot) and creates a feedback loop via
IPV6_MULTICAST_LOOP. If you want bidirectional flow, use two distinct
topics (the MQTT `set/...` + `state/...` pattern) wired through
automations.
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

DEPENDENCIES = ["mpubsub"]

CONF_PARENT_ID = "mpubsub_id"

MODE_SUBSCRIBE = "subscribe"
MODE_PUBLISH = "publish"
MODES = [MODE_SUBSCRIBE, MODE_PUBLISH]

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
    cg.add(var.set_subscribe(mode == MODE_SUBSCRIBE))
    cg.add(var.set_publish(mode == MODE_PUBLISH))
