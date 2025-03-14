# Copyright 2019 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Common functionality relating to the implementation of mycroft skills."""
import itertools
import logging
import re
import sys
import traceback
import typing
from copy import deepcopy
from enum import Enum
from itertools import chain
from os import walk
from os.path import abspath, basename, dirname, exists, join
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from typing import Any, Dict, Optional, Sequence, Tuple, Union
from unittest.mock import MagicMock
from uuid import uuid4

import mycroft.dialog
from adapt.intent import Intent, IntentBuilder
from mycroft.api import DeviceApi
from mycroft.configuration import Configuration
from mycroft.dialog import load_dialogs
from mycroft.enclosure.gui import SkillGUI
from mycroft.filesystem import FileSystemAccess
from mycroft.messagebus.message import Message
from mycroft.util.file_utils import resolve_resource_file
from mycroft.util.log import LOG
from mycroft.util.string_utils import camel_case_split
from xdg import BaseDirectory

from ..event_scheduler import EventSchedulerInterface
from ..intent_service_interface import IntentServiceInterface
from ..settings import get_local_settings, save_settings
from ..skill_data import ResourceFile, SkillResources, munge_intent_parser, munge_regex
from .event_container import (
    EventContainer,
    create_wrapper,
    get_handler_name,
    unmunge_message,
)
from .skill_control import SkillControl

SessionDialogDataType = Optional[Dict[str, Any]]
SessionDialogType = Union[str, Tuple[str, SessionDialogDataType]]
SessionDialogsType = Union[SessionDialogType, Sequence[SessionDialogType]]

SessionGuiDataType = Optional[Dict[str, Any]]
SessionGuiType = Union[str, Tuple[str, SessionGuiDataType]]
SessionGuisType = Union[SessionGuiType, Sequence[SessionGuiType]]


class GuiClear(str, Enum):
    AUTO = "auto"
    ON_IDLE = "on_idle"
    NEVER = "never"
    AT_START = "at_start"
    AT_END = "at_end"


class MessageSend(str, Enum):
    AT_START = "at_start"
    AT_END = "at_end"


def simple_trace(stack_trace):
    """Generate a simplified traceback.

    Args:
        stack_trace: Stack trace to simplify

    Returns: (str) Simplified stack trace.
    """
    stack_trace = stack_trace[:-1]
    tb = "Traceback:\n"
    for line in stack_trace:
        if line.strip():
            tb += line
    return tb


def get_non_properties(obj):
    """Get attibutes that are not properties from object.

    Will return members of object class along with bases down to MycroftSkill.

    Args:
        obj: object to scan

    Returns:
        Set of attributes that are not a property.
    """

    def check_class(cls):
        """Find all non-properties in a class."""
        # Current class
        d = cls.__dict__
        np = [k for k in d if not isinstance(d[k], property)]
        # Recurse through base classes excluding MycroftSkill and object
        for b in [b for b in cls.__bases__ if b not in (object, MycroftSkill)]:
            np += check_class(b)
        return np

    return set(check_class(obj.__class__))


class MycroftSkill:
    """Base class for mycroft skills providing common behaviour and parameters
    to all Skill implementations.

    For information on how to get started with creating mycroft skills see
    https://mycroft.ai/documentation/skills/introduction-developing-skills/

    Args:
        name (str): skill name
        bus (MycroftWebsocketClient): Optional bus connection
        use_settings (bool): Set to false to not use skill settings at all
    """

    _resources = None

    def __init__(self, skill_id: str, name=None, bus=None, use_settings=True):
        self.name = name or self.__class__.__name__
        self.settings_meta = None  # set when skill is loaded in SkillLoader
        self.skill_id = skill_id
        self.skill_service_initializing = False

        # For get_response
        self._response_queue: "Queue[str]" = Queue()
        self._mycroft_session_id: typing.Optional[str] = None
        self._session_lock = Lock()

        # Get directory of skill
        #: Member variable containing the absolute path of the skill's root
        #: directory. E.g. /opt/mycroft/skills/my-skill.me/
        self.root_dir = dirname(abspath(sys.modules[self.__module__].__file__))

        self.gui = SkillGUI(self)

        self._bus = None
        self._enclosure = MagicMock()

        #: Mycroft global configuration. (dict)
        self.config_core = Configuration.get()

        self.settings = None
        self.settings_write_path = None

        if use_settings:
            self._init_settings()

        #: Set to register a callback method that will be called every time
        #: the skills settings are updated. The referenced method should
        #: include any logic needed to handle the updated settings.
        self.settings_change_callback = None

        self.dialog_renderer = None

        #: Filesystem access to skill specific folder.
        #: See mycroft.filesystem for details.
        self.file_system = FileSystemAccess(join("skills", self.skill_id))

        self.log = logging.getLogger(self.skill_id)  #: Skill logger instance
        self.reload_skill = True  #: allow reloading (default True)

        self.events = EventContainer(bus)
        self.voc_match_cache = {}

        # Delegator classes
        self.event_scheduler = EventSchedulerInterface(self.skill_id)
        self.intent_service = IntentServiceInterface()

        # Skill Public API
        self.public_api = {}

        self.skill_control = SkillControl()

        # Unique id generated for every started/ended
        self._activity_id: str = ""

        # Session id from last speak()
        self._tts_session_id: typing.Optional[str] = None
        self._tts_speak_finished = Event()

        # Should be last to avoid race conditions where event handlers try to
        # access attributes that have yet to be initialized.
        self.bind(bus)

    def change_state(self, new_state):
        """change skill state to new value.
        does nothing except log a warning
        if the new state is invalid"""
        self.log.debug(
            "change_state() skill:%s - changing state from %s to %s"
            % (self.skill_id, self.skill_control.state, new_state)
        )

        if self.skill_control.states is None:
            return

        if new_state not in self.skill_control.states:
            self.log.warning(
                "invalid state change, from %s to %s"
                % (self.skill_control.state, new_state)
            )
            return

        if new_state != self.skill_control.state:

            for intent in self.skill_control.states[self.skill_control.state]:
                self.disable_intent(intent)

            self.skill_control.state = new_state

            for intent in self.skill_control.states[self.skill_control.state]:
                self.enable_intent(intent)

            if new_state == "inactive":
                self.log.debug("send msg: deactivate %s" % (self.skill_id,))
                self.bus.emit(
                    Message("deactivate_skill_request", {"skill_id": self.skill_id})
                )

            if new_state == "active":
                self.log.debug("send msg: activate %s" % (self.skill_id,))
                self.bus.emit(
                    Message(
                        "active_skill_request",
                        {
                            "skill_id": self.skill_id,
                            "skill_cat": self.skill_control.category,
                        },
                    )
                )

    def _init_settings(self):
        """Setup skill settings."""
        self.settings_write_path = Path(
            BaseDirectory.save_config_path("mycroft", "skills", self.skill_id)
        )
        settings_read_path = self.settings_write_path
        self.settings = get_local_settings(settings_read_path, self.skill_id)
        self._initial_settings = deepcopy(self.settings)

    @property
    def enclosure(self):
        if self._enclosure:
            return self._enclosure
        else:
            LOG.error(
                "Skill not fully initialized. Move code "
                + "from  __init__() to initialize() to correct this."
            )
            LOG.error(simple_trace(traceback.format_stack()))
            raise Exception("Accessed MycroftSkill.enclosure in __init__")

    @property
    def bus(self):
        if self._bus:
            return self._bus
        else:
            LOG.error(
                "Skill not fully initialized. Move code "
                + "from __init__() to initialize() to correct this."
            )
            LOG.error(simple_trace(traceback.format_stack()))
            raise Exception("Accessed MycroftSkill.bus in __init__")

    @property
    def location(self):
        """Get the JSON data struction holding location information."""
        # TODO: Allow Enclosure to override this for devices that
        # contain a GPS.
        return self.config_core.get("location")

    @property
    def location_pretty(self):
        """Get a more 'human' version of the location as a string."""
        loc = self.location
        if type(loc) is dict and loc["city"]:
            return loc["city"]["name"]
        return None

    @property
    def location_timezone(self):
        """Get the timezone code, such as 'America/Los_Angeles'"""
        loc = self.location
        if type(loc) is dict and loc["timezone"]:
            return loc["timezone"]["code"]
        return None

    @property
    def lang(self):
        """Get the configured language."""
        return self.config_core.get("lang")

    @property
    def alphanumeric_skill_id(self):
        return "".join(char if char.isalnum() else "_" for char in str(self.skill_id))

    @property
    def resources(self):
        if self._resources is None:
            self._resources = SkillResources(
                self.root_dir, self.lang, self.dialog_renderer
            )

        return self._resources

    def bind(self, bus):
        """Register messagebus emitter with skill.

        Args:
            bus: Mycroft messagebus connection
        """
        if bus:
            self._bus = bus
            self.events.set_bus(bus)
            self.intent_service.set_bus(bus)
            self.event_scheduler.set_bus(bus)
            self.event_scheduler.set_id(self.skill_id)
            self._register_system_event_handlers()
            # Initialize the SkillGui
            self.gui.setup_default_handlers()

            self._register_public_api()

            self._bus.on("mycroft.skill-response", self.__handle_skill_response)
            self._bus.on("mycroft.gui.handle-idle", self.__handle_gui_idle)

    def _register_public_api(self):
        """Find and register api methods.
        Api methods has been tagged with the api_method member, for each
        method where this is found the method a message bus handler is
        registered.
        Finally create a handler for fetching the api info from any requesting
        skill.
        """

        def wrap_method(func):
            """Boiler plate for returning the response to the sender."""

            def wrapper(message):
                args = message.data.get("args", [])
                kwargs = message.data.get("kwargs", {})
                result = func(*args, **kwargs)
                self.bus.emit(message.response(data={"result": result}))

            return wrapper

        methods = [
            attr_name
            for attr_name in get_non_properties(self)
            if hasattr(getattr(self, attr_name), "__name__")
        ]

        for attr_name in methods:
            method = getattr(self, attr_name)

            if hasattr(method, "api_method"):
                doc = method.__doc__ or ""
                name = method.__name__
                self.public_api[name] = {
                    "help": doc,
                    "type": "{}.{}".format(self.skill_id, name),
                    "func": method,
                }
        for key in self.public_api:
            if "type" in self.public_api[key] and "func" in self.public_api[key]:
                LOG.debug(
                    "Adding api method: " "{}".format(self.public_api[key]["type"])
                )

                # remove the function member since it shouldn't be
                # reused and can't be sent over the messagebus
                func = self.public_api[key].pop("func")
                self.add_event(self.public_api[key]["type"], wrap_method(func))

        if self.public_api:
            self.add_event("{}.public_api".format(self.skill_id), self._send_public_api)

    def _register_system_event_handlers(self):
        """Add all events allowing the standard interaction with the Mycroft
        system.
        """
        self.add_event("mycroft.skill.stop", self.__handle_skill_stop)
        self.add_event("mycroft.skills.initialized", self.handle_skills_initialized)
        self.add_event("mycroft.skill.enable_intent", self.handle_enable_intent)
        self.add_event("mycroft.skill.disable_intent", self.handle_disable_intent)
        self.add_event("mycroft.skill.set_cross_context", self.handle_set_cross_context)
        self.add_event(
            "mycroft.skill.remove_cross_context", self.handle_remove_cross_context
        )
        self.events.add("mycroft.skills.settings.changed", self.handle_settings_change)

    def handle_skills_initialized(self, _):
        self.skill_service_initializing = False

    def handle_settings_change(self, message):
        """Update settings if the remote settings changes apply to this skill.

        The skill settings downloader uses a single API call to retrieve the
        settings for all skills.  This is done to limit the number API calls.
        A "mycroft.skills.settings.changed" event is emitted for each skill
        that had their settings changed.  Only update this skill's settings
        if its remote settings were among those changed
        """
        remote_settings = message.data.get(self.skill_id)
        if remote_settings is not None:
            LOG.info("Updating settings for skill " + self.skill_id)
            self.settings.update(**remote_settings)
            save_settings(self.settings_write_path, self.settings)
            if self.settings_change_callback is not None:
                self.settings_change_callback()

    def detach(self):
        for (name, _) in self.intent_service:
            name = "{}:{}".format(self.skill_id, name)
            self.intent_service.detach_intent(name)

    def initialize(self):
        """Perform any final setup needed for the skill.

        Invoked after the skill is fully constructed and registered with the
        system.
        """
        pass

    def _send_public_api(self, message):
        """Respond with the skill's public api."""
        self.bus.emit(message.response(data=self.public_api))

    def get_intro_message(self):
        """Get a message to speak on first load of the skill.

        Useful for post-install setup instructions.

        Returns:
            str: message that will be spoken to the user
        """
        return None

    def voc_match(self, utt, voc_filename, lang=None, exact=False):
        """Determine if the given utterance contains the vocabulary provided.

        By default the method checks if the utterance contains the given vocab
        thereby allowing the user to say things like "yes, please" and still
        match against "Yes.voc" containing only "yes". An exact match can be
        requested.

        The method first checks in the current Skill's .voc files and secondly
        in the "res/text" folder of mycroft-core. The result is cached to
        avoid hitting the disk each time the method is called.

        Args:
            utt (str): Utterance to be tested
            voc_filename (str): Name of vocabulary file (e.g. 'yes' for
                                'res/text/en-us/yes.voc')
            lang (str): Language code, defaults to self.long
            exact (bool): Whether the vocab must exactly match the utterance

        Returns:
            bool: True if the utterance has the given vocabulary it
        """
        match = False
        lang = lang or self.lang
        cache_key = lang + voc_filename
        if cache_key not in self.voc_match_cache:
            vocab = self.resources.load_vocabulary_file(voc_filename)
            self.voc_match_cache[cache_key] = list(chain(*vocab))
        if utt:
            if exact:
                # Check for exact match
                match = any(i.strip() == utt for i in self.voc_match_cache[cache_key])
            else:
                # Check for matches against complete words
                match = any(
                    [
                        re.match(r".*\b" + i + r"\b.*", utt)
                        for i in self.voc_match_cache[cache_key]
                    ]
                )

        return match

    def report_metric(self, name, data):
        """Report a skill metric to the Mycroft servers.

        Args:
            name (str): Name of metric. Must use only letters and hyphens
            data (dict): JSON dictionary to report. Must be valid JSON
        """
        # report_metric("{}:{}".format(basename(self.root_dir), name), data)

    def send_email(self, title, body):
        """Send an email to the registered user's email.

        Args:
            title (str): Title of email
            body  (str): HTML body of email. This supports
                         simple HTML like bold and italics
        """
        DeviceApi().send_email(title, body, basename(self.root_dir))

    def make_active(self):
        """Bump skill to active_skill list in intent_service.

        This enables converse method to be called even without skill being
        used in last 5 minutes.
        """
        if self.skill_control.category == "undefined":
            self.bus.emit(Message("active_skill_request", {"skill_id": self.skill_id}))

    def _register_decorated(self):
        """Register all intent handlers that are decorated with an intent.

        Looks for all functions that have been marked by a decorator
        and read the intent data from them.  The intent handlers aren't the
        only decorators used.  Skip properties as calling getattr on them
        executes the code which may have unintended side-effects
        """
        for attr_name in get_non_properties(self):
            method = getattr(self, attr_name)
            if hasattr(method, "intents"):
                for intent in getattr(method, "intents"):
                    self.register_intent(intent, method)

            if hasattr(method, "intent_files"):
                for intent_file in getattr(method, "intent_files"):
                    self.register_intent_file(intent_file, method)

    def translate(self, text, data=None):
        """Deprecated method for translating a dialog file."""
        return self.resources.render_dialog(text, data)

    def find_resource(self, res_name, res_dirname=None):
        """Find a resource file.

        Searches for the given filename using this scheme:
            1. Search the resource lang directory:
                <skill>/<res_dirname>/<lang>/<res_name>
            2. Search the resource directory:
                <skill>/<res_dirname>/<res_name>

            3. Search the locale lang directory or other subdirectory:
                <skill>/locale/<lang>/<res_name> or
                <skill>/locale/<lang>/.../<res_name>

        Args:
            res_name (string): The resource name to be found
            res_dirname (string, optional): A skill resource directory, such
                                            'dialog', 'vocab', 'regex' or 'ui'.
                                            Defaults to None.

        Returns:
            string: The full path to the resource file or None if not found
        """
        result = self._find_resource(res_name, self.lang, res_dirname)
        if not result and self.lang != "en-us":
            # when resource not found try fallback to en-us
            LOG.warning(
                "Resource '{}' for lang '{}' not found: trying 'en-us'".format(
                    res_name, self.lang
                )
            )
            result = self._find_resource(res_name, "en-us", res_dirname)
        return result

    def _find_resource(self, res_name, lang, res_dirname=None):
        """Finds a resource by name, lang and dir"""
        if res_dirname:
            # Try the old translated directory (dialog/vocab/regex)
            path = join(self.root_dir, res_dirname, lang, res_name)
            if exists(path):
                return path

            # Try old-style non-translated resource
            path = join(self.root_dir, res_dirname, res_name)
            if exists(path):
                return path

        # New scheme:  search for res_name under the 'locale' folder
        root_path = join(self.root_dir, "locale", lang)
        for path, _, files in walk(root_path):
            if res_name in files:
                return join(path, res_name)

        # Not found
        return None

    def translate_namedvalues(self, name, delim=","):
        """Deprecated method for translating a name/value file."""
        return self.resources.load_named_value_file(name, delim)

    def translate_list(self, list_name, data=None):
        """Deprecated method for translating a list."""
        return self.resources.load_list_file(list_name, data)

    def translate_template(self, template_name, data=None):
        """Deprecated method for translating a template file"""
        return self.resources.load_template_file(template_name, data)

    def add_event(self, name, handler, handler_info=None, once=False):
        """Create event handler for executing intent or other event.

        Args:
            name (string): IntentParser name
            handler (func): Method to call
            handler_info (string): Base message when reporting skill event
                                   handler status on messagebus.
            once (bool, optional): Event handler will be removed after it has
                                   been run once.
        """
        skill_data = {"name": get_handler_name(handler), "skill_id": self.skill_id}

        def on_error(e):
            """Speak and log the error."""
            # Convert "MyFancySkill" to "My Fancy Skill" for speaking
            handler_name = camel_case_split(self.name)
            msg_data = {"skill": handler_name}
            msg = mycroft.dialog.get("skill.error", self.lang, msg_data)
            # self.speak(msg)
            LOG.exception(msg)
            # append exception information in message
            skill_data["exception"] = repr(e)

        def on_start(message):
            """Indicate that the skill handler is starting."""
            if handler_info:
                # Indicate that the skill handler is starting if requested
                msg_type = handler_info + ".start"
                self.bus.emit(message.forward(msg_type, skill_data))

        def on_end(message):
            """Store settings and indicate that the skill handler has completed"""
            if self.settings != self._initial_settings:
                save_settings(self.settings_write_path, self.settings)
                self._initial_settings = deepcopy(self.settings)
            if handler_info:
                msg_type = handler_info + ".complete"
                self.bus.emit(message.forward(msg_type, skill_data))

        wrapper = create_wrapper(handler, self.skill_id, on_start, on_end, on_error)
        return self.events.add(name, wrapper, once)

    def remove_event(self, name):
        """Removes an event from bus emitter and events list.

        Args:
            name (string): Name of Intent or Scheduler Event
        Returns:
            bool: True if found and removed, False if not found
        """
        return self.events.remove(name)

    def _add_intent_handler(self, name, handler):
        def _handle_intent(message: Message):
            self._mycroft_session_id = message.data.get("mycroft_session_id")
            self.log.debug(
                "Handling %s with skill %s (session=%s)",
                name,
                self.skill_id,
                self._mycroft_session_id,
            )

            result_message: Optional[Message] = None
            try:
                self.acknowledge()
                message = unmunge_message(message, self.skill_id)
                result_message = handler(message)
            except Exception:
                LOG.exception("Error in intent handler: %s", name)

                # Speak error
                self.emit_start_session(
                    dialog=("skill.error", {"skill": camel_case_split(self.name)})
                )

            if result_message is None:
                result_message = self.end_session()

            self.bus.emit(result_message)

        self._bus.on(name, _handle_intent)

    def _register_adapt_intent(self, intent_parser, handler):
        """Register an adapt intent.

        Args:
            intent_parser: Intent object to parse utterance for the handler.
            handler (func): function to register with intent
        """
        # Default to the handler's function name if none given
        name = intent_parser.name or handler.__name__
        munge_intent_parser(intent_parser, name, self.skill_id)
        self.intent_service.register_adapt_intent(name, intent_parser)
        if handler:
            # self.add_event(
            #     intent_parser.name,
            #     handler,
            #     "mycroft.skill.handler",
            # )
            self._add_intent_handler(intent_parser.name, handler)

    def register_intent(self, intent_parser, handler):
        """Register an Intent with the intent service.

        Args:
            intent_parser: Intent, IntentBuilder object or padatious intent
                           file to parse utterance for the handler.
            handler (func): function to register with intent
        """
        if isinstance(intent_parser, IntentBuilder):
            intent_parser = intent_parser.build()
        if isinstance(intent_parser, str) and intent_parser.endswith(".intent"):
            return self.register_intent_file(intent_parser, handler)
        if isinstance(intent_parser, str) and intent_parser.endswith(".rx"):
            return self.register_regex_intent(intent_parser, handler)
        elif not isinstance(intent_parser, Intent):
            raise ValueError('"' + str(intent_parser) + '" is not an Intent')

        return self._register_adapt_intent(intent_parser, handler)

    def register_intent_file(self, intent_file, handler):
        """Register an Intent file with the intent service.

        For example:
            food.order.intent:
                Order some {food}.
                Order some {food} from {place}.
                I'm hungry.
                Grab some {food} from {place}.

        Optionally, you can also use <register_entity_file>
        to specify some examples of {food} and {place}

        In addition, instead of writing out multiple variations
        of the same sentence you can write:
            food.order.intent:
                (Order | Grab) some {food} (from {place} | ).
                I'm hungry.

        Args:
            intent_file: name of file that contains example queries
                         that should activate the intent.  Must end with
                         '.intent'
            handler:     function to register with intent
        """
        name = "{}:{}".format(self.skill_id, intent_file)
        resource_file = ResourceFile(self.resources.types.intent, intent_file)
        if resource_file.file_path is None:
            raise FileNotFoundError('Unable to find "{}"'.format(intent_file))
        self.intent_service.register_padatious_intent(
            name, str(resource_file.file_path)
        )
        if handler:
            # self.add_event(
            #     name,
            #     handler,
            #     "mycroft.skill.handler",
            # )
            self._add_intent_handler(name, handler)

    def register_entity_file(self, entity_file):
        """Register an Entity file with the intent service.

        An Entity file lists the exact values that an entity can hold.
        For example:
            ask.day.intent:
                Is it {weekend}?
            weekend.entity:
                Saturday
                Sunday

        Args:
            entity_file (string): name of file that contains examples of an
                                  entity.
        """
        entity = ResourceFile(self.resources.types.entity, entity_file)
        if entity.file_path is None:
            raise FileNotFoundError('Unable to find "{}"'.format(entity_file))

        name = "{}:{}".format(self.skill_id, entity_file)
        self.intent_service.register_padatious_entity(name, str(entity.file_path))

    def register_regex_intent(self, intent_file, handler):
        """Register a regular expression pattern with the intent service

        Args:
            intent_file: path to file with regex pattern that matches the entire
                         utterance
            handler:     function to register with intent
        """
        regex = ResourceFile(self.resources.types.regex, intent_file)
        if regex.file_path is None:
            raise FileNotFoundError('Unable to find "{}"'.format(intent_file))

        name = "{}:{}".format(self.skill_id, intent_file)
        self.intent_service.register_regex_intent(name, str(regex.file_path))
        if handler:
            # self.add_event(name, handler, "mycroft.skill.handler")
            self._add_intent_handler(name, handler)

    def handle_enable_intent(self, message):
        """Listener to enable a registered intent if it belongs to this skill."""
        intent_name = message.data["intent_name"]
        for (name, _) in self.intent_service:
            if name == intent_name:
                return self.enable_intent(intent_name)

    def handle_disable_intent(self, message):
        """Listener to disable a registered intent if it belongs to this skill."""
        intent_name = message.data["intent_name"]
        for (name, _) in self.intent_service:
            if name == intent_name:
                return self.disable_intent(intent_name)

    def disable_intent(self, intent_name):
        """Disable a registered intent if it belongs to this skill.

        Args:
            intent_name (string): name of the intent to be disabled

        Returns:
                bool: True if disabled, False if it wasn't registered
        """
        if intent_name in self.intent_service:
            LOG.debug("Disabling intent " + intent_name)
            name = "{}:{}".format(self.skill_id, intent_name)
            self.intent_service.detach_intent(name)
            return True
        else:
            LOG.error(
                "Could not disable "
                "{}, it hasn't been registered.".format(intent_name)
            )
            return False

    def enable_intent(self, intent_name):
        """(Re)Enable a registered intent if it belongs to this skill.

        Args:
            intent_name: name of the intent to be enabled

        Returns:
            bool: True if enabled, False if it wasn't registered
        """
        intent = self.intent_service.get_intent(intent_name)
        if intent:
            if ".intent" in intent_name:
                self.register_intent_file(intent_name, None)
            else:
                intent.name = intent_name
                self.register_intent(intent, None)
            LOG.debug("Enabling intent {}".format(intent_name))
            return True
        else:
            LOG.error(
                "Could not enable " "{}, it hasn't been registered.".format(intent_name)
            )
            return False

    def set_context(self, context, word="", origin=""):
        """Add context to intent service

        Args:
            context:    Keyword
            word:       word connected to keyword
            origin:     origin of context
        """
        if not isinstance(context, str):
            raise ValueError("Context should be a string")
        if not isinstance(word, str):
            raise ValueError("Word should be a string")

        context = self.alphanumeric_skill_id + context
        self.intent_service.set_adapt_context(context, word, origin)

    def handle_set_cross_context(self, message):
        """Add global context to intent service."""
        context = message.data.get("context")
        word = message.data.get("word")
        origin = message.data.get("origin")

        self.set_context(context, word, origin)

    def handle_remove_cross_context(self, message):
        """Remove global context from intent service."""
        context = message.data.get("context")
        self.remove_context(context)

    def set_cross_skill_context(self, context, word=""):
        """Tell all skills to add a context to intent service

        Args:
            context:    Keyword
            word:       word connected to keyword
        """
        self.bus.emit(
            Message(
                "mycroft.skill.set_cross_context",
                {"context": context, "word": word, "origin": self.skill_id},
            )
        )

    def remove_cross_skill_context(self, context):
        """Tell all skills to remove a keyword from the context manager."""
        if not isinstance(context, str):
            raise ValueError("context should be a string")
        self.bus.emit(
            Message("mycroft.skill.remove_cross_context", {"context": context})
        )

    def remove_context(self, context):
        """Remove a keyword from the context manager."""
        if not isinstance(context, str):
            raise ValueError("context should be a string")
        context = self.alphanumeric_skill_id + context
        self.intent_service.remove_adapt_context(context)

    def register_vocabulary(self, entity, entity_type):
        """Register a word to a keyword

        Args:
            entity:         word to register
            entity_type:    Intent handler entity to tie the word to
        """
        keyword_type = self.alphanumeric_skill_id + entity_type
        self.intent_service.register_adapt_keyword(keyword_type, entity)

    def register_regex(self, regex_str):
        """Register a new regex.
        Args:
            regex_str: Regex string
        """
        regex = munge_regex(regex_str, self.skill_id)
        re.compile(regex)  # validate regex
        self.intent_service.register_adapt_regex(regex)

    def acknowledge(self):
        """Acknowledge a successful request.

        This method plays a sound to acknowledge a request that does not
        require a verbal response. This is intended to provide simple feedback
        to the user that their request was handled successfully.
        """
        acknowledge = self.config_core.get("sounds").get("acknowledge")
        if acknowledge:
            audio_file = resolve_resource_file(acknowledge)

            if not audio_file:
                LOG.warning("Could not find 'acknowledge' audio file!")
                return

            uri = f"file://{audio_file}"
            self.play_sound_uri(uri)

    def load_data_files(self):
        """Called by the skill loader to load intents, dialogs, etc."""
        self.init_dialog()
        self.load_vocab_files()
        self.load_regex_files()

    def init_dialog(self):
        # If "<skill>/dialog/<lang>" exists, load from there.  Otherwise
        # load dialog from "<skill>/locale/<lang>"
        dialog_dir = join(self.root_dir, "dialog", self.lang)
        if exists(dialog_dir):
            self.dialog_renderer = load_dialogs(dialog_dir)
        elif exists(join(self.root_dir, "locale", self.lang)):
            locale_path = join(self.root_dir, "locale", self.lang)
            self.dialog_renderer = load_dialogs(locale_path)
        else:
            LOG.debug("No dialog loaded")
        self.resources.dialog_renderer = self.dialog_renderer

    def load_vocab_files(self):
        """Load vocab files found under skill's root directory."""
        if self.resources.types.vocabulary.base_directory is None:
            self.log.info("Skill has no vocabulary")
        else:
            skill_vocabulary = self.resources.load_skill_vocabulary(
                self.alphanumeric_skill_id
            )
            # For each found intent register the default along with any aliases
            for vocab_type in skill_vocabulary:
                for line in skill_vocabulary[vocab_type]:
                    entity = line[0]
                    aliases = line[1:]
                    self.intent_service.register_adapt_keyword(
                        vocab_type, entity, aliases
                    )

    def load_regex_files(self):
        """Load regex files found under the skill directory."""
        if self.resources.types.regex.base_directory is not None:
            regexes = self.resources.load_skill_regex(self.alphanumeric_skill_id)
            for regex in regexes:
                self.intent_service.register_adapt_regex(regex)

    def __handle_skill_stop(self, message: Message):
        skill_id = message.data.get("skill_id")
        if skill_id == self.skill_id:
            self.log.debug("Handling stop in skill: %s", self.skill_id)
            self._mycroft_session_id = message.data.get("mycroft_session_id")

            result_message: Optional[Message] = None
            try:
                result_message = self.stop()
            except Exception:
                self.log.exception("Error handling stop")

            if result_message is None:
                result_message = self.end_session()

            self.bus.emit(result_message)

    def stop(self) -> Optional[Message]:
        """Optional method implemented by subclass."""
        return self.end_session(gui_clear=GuiClear.AT_END)

    def shutdown(self):
        """Optional shutdown proceedure implemented by subclass.

        This method is intended to be called during the skill process
        termination. The skill implementation must shutdown all processes and
        operations in execution.
        """
        pass

    def default_shutdown(self):
        """Parent function called internally to shut down everything.

        Shuts down known entities and calls skill specific shutdown method.
        """
        try:
            self.shutdown()
        except Exception as e:
            LOG.error(
                "Skill specific shutdown function encountered "
                "an error: {}".format(repr(e))
            )

        self.settings_change_callback = None

        # Store settings
        if self.settings != self._initial_settings and Path(self.root_dir).exists():
            save_settings(self.settings_write_path, self.settings)

        # if self.settings_meta:
        #     self.settings_meta.stop()

        # Clear skill from gui
        self.gui.shutdown()

        # removing events
        self.event_scheduler.shutdown()
        self.events.clear()

        self.bus.emit(Message("detach_skill", {"skill_id": str(self.skill_id) + ":"}))
        try:
            self.stop()
        except Exception:
            LOG.error("Failed to stop skill: {}".format(self.skill_id), exc_info=True)

    def schedule_event(self, handler, when, data=None, name=None, context=None):
        """Schedule a single-shot event.

        Args:
            handler:               method to be called
            when (datetime/int/float):   datetime (in system timezone) or
                                   number of seconds in the future when the
                                   handler should be called
            data (dict, optional): data to send when the handler is called
            name (str, optional):  reference name
                                   NOTE: This will not warn or replace a
                                   previously scheduled event of the same
                                   name.
            context (dict, optional): context (dict, optional): message
                                      context to send when the handler
                                      is called
        """
        context = {}
        return self.event_scheduler.schedule_event(
            handler, when, data, name, context=context
        )

    def schedule_repeating_event(
        self, handler, when, frequency, data=None, name=None, context=None
    ):
        """Schedule a repeating event.

        Args:
            handler:                method to be called
            when (datetime):        time (in system timezone) for first
                                    calling the handler, or None to
                                    initially trigger <frequency> seconds
                                    from now
            frequency (float/int):  time in seconds between calls
            data (dict, optional):  data to send when the handler is called
            name (str, optional):   reference name, must be unique
            context (dict, optional): context (dict, optional): message
                                      context to send when the handler
                                      is called
        """
        context = {}
        return self.event_scheduler.schedule_repeating_event(
            handler, when, frequency, data, name, context=context
        )

    def update_scheduled_event(self, name, data=None):
        """Change data of event.

        Args:
            name (str): reference name of event (from original scheduling)
            data (dict): event data
        """
        return self.event_scheduler.update_scheduled_event(name, data)

    def cancel_scheduled_event(self, name):
        """Cancel a pending event. The event will no longer be scheduled
        to be executed

        Args:
            name (str): reference name of event (from original scheduling)
        """
        return self.event_scheduler.cancel_scheduled_event(name)

    def get_scheduled_event_status(self, name):
        """Get scheduled event data and return the amount of time left

        Args:
            name (str): reference name of event (from original scheduling)

        Returns:
            int: the time left in seconds

        Raises:
            Exception: Raised if event is not found
        """
        return self.event_scheduler.get_scheduled_event_status(name)

    def cancel_all_repeating_events(self):
        """Cancel any repeating events started by the skill."""
        return self.event_scheduler.cancel_all_repeating_events()

    def play_sound_uri(self, uri: str):
        self.bus.emit(
            Message(
                "mycroft.audio.play-sound",
                data={"uri": uri, "mycroft_session_id": self._mycroft_session_id},
            )
        )

    # -------------------------------------------------------------------------

    def update_gui_values(
        self, page: str, data: Dict[str, Any], overwrite: bool = True
    ):
        self.bus.emit(
            Message(
                "gui.value.set",
                data={
                    "namespace": f"{self.skill_id}.{page}",
                    "data": data,
                    "overwrite": overwrite,
                },
            )
        )

    def _build_actions(
        self,
        dialog: Optional[SessionDialogsType] = None,
        speak: Optional[str] = None,
        speak_wait: bool = True,
        gui: Optional[SessionGuisType] = None,
        gui_clear: GuiClear = GuiClear.AUTO,
        audio_alert: Optional[str] = None,
        music_uri: Optional[str] = None,
        message: Optional[Message] = None,
        message_send: MessageSend = MessageSend.AT_START,
        message_delay: float = 0.0,
        expect_response: bool = False,
    ):
        # Action ordering is fixed:
        # 1. Send message (if "at_start")
        # 2. Clear gui (if "at_start")
        # 3. Play audio alert
        # 4. Show gui page(s)
        # 5. Speak dialog(s) or text
        # 6. Clear gui or set idle timeout
        actions = []

        if expect_response and (gui_clear == GuiClear.AUTO):
            # Don't clear GUI if a response is needed from the user
            gui_clear = GuiClear.NEVER

        # 1. Send message
        if (message is not None) and (message_send == MessageSend.AT_START):
            actions.append(
                {
                    "type": "message",
                    "message_type": message.msg_type,
                    "data": {
                        # Automatically add session id
                        "mycroft_session_id": self._mycroft_session_id,
                        **message.data,
                    },
                }
            )

        # 2. Clear gui (if "at_start")
        if gui_clear == GuiClear.AT_START:
            actions.append({"type": "clear_display"})

        # 3. Play audio alert
        if audio_alert:
            actions.append({"type": "audio_alert", "uri": audio_alert, "wait": True})

        # 4. Show gui page(s)
        guis = []
        if gui is not None:
            if isinstance(gui, (str, tuple)):
                # Single gui
                guis = [gui]
            else:
                guis = list(gui)

        # 5. Speak dialog(s) or text
        dialogs = []
        if dialog is not None:
            if isinstance(dialog, (str, tuple)):
                # Single dialog
                dialogs = [dialog]
            else:
                dialogs = list(dialog)

        # Interleave dialog/gui pages
        for maybe_dialog, maybe_gui in itertools.zip_longest(dialogs, guis):
            if maybe_gui is not None:
                if isinstance(maybe_gui, str):
                    gui_page, gui_data = maybe_gui, None
                else:
                    gui_page, gui_data = maybe_gui

                actions.append(
                    {
                        "type": "show_page",
                        "page": "file://" + self.find_resource(gui_page, "ui"),
                        "data": gui_data,
                        "namespace": f"{self.skill_id}.{gui_page}",
                    }
                )

            if maybe_dialog is not None:
                if isinstance(maybe_dialog, str):
                    dialog_name, dialog_data = maybe_dialog, {}
                else:
                    dialog_name, dialog_data = maybe_dialog

                utterance = self.dialog_renderer.render(dialog_name, dialog_data)
                actions.append(
                    {
                        "type": "speak",
                        "utterance": utterance,
                        "dialog": dialog_name,
                        "wait": speak_wait,
                    }
                )

        if speak is not None:
            actions.append(
                {
                    "type": "speak",
                    "utterance": speak,
                    "wait": speak_wait,
                }
            )

        if (message is not None) and (message_send == MessageSend.AT_END):
            actions.append(
                {
                    "type": "message",
                    "message_type": message.msg_type,
                    "delay": message_delay,
                    "data": {
                        # Automatically add session id
                        "mycroft_session_id": self._mycroft_session_id,
                        **message.data,
                    },
                }
            )

        if music_uri:
            actions.append({"type": "stream_music", "uri": music_uri})

        if expect_response:
            actions.append({"type": "get_response"})

        if gui_clear == GuiClear.AUTO:
            if guis:
                if dialogs or (speak is not None):
                    # TTS, wait for speak
                    gui_clear = GuiClear.AT_END
                else:
                    # No TTS, so time out on idle
                    gui_clear = GuiClear.ON_IDLE
            else:
                # No GUI, don't clear
                gui_clear = GuiClear.NEVER

        if gui_clear == GuiClear.AT_END:
            actions.append({"type": "clear_display"})
        elif gui_clear == GuiClear.ON_IDLE:
            actions.append({"type": "wait_for_idle"})

        return actions

    def emit_start_session(
        self,
        dialog: Optional[SessionDialogsType] = None,
        speak: Optional[str] = None,
        speak_wait: bool = True,
        gui: Optional[SessionGuisType] = None,
        gui_clear: GuiClear = GuiClear.AUTO,
        audio_alert: Optional[str] = None,
        music_uri: Optional[str] = None,
        expect_response: bool = False,
        message: Optional[Message] = None,
        continue_session: bool = False,
        message_send: MessageSend = MessageSend.AT_START,
        message_delay: float = 0.0,
        mycroft_session_id: Optional[str] = None,
    ) -> str:
        if mycroft_session_id is None:
            mycroft_session_id = str(uuid4())

        message = Message(
            "mycroft.session.start",
            data={
                "mycroft_session_id": mycroft_session_id,
                "skill_id": self.skill_id,
                "actions": self._build_actions(
                    dialog=dialog,
                    speak=speak,
                    speak_wait=speak_wait,
                    gui=gui,
                    gui_clear=gui_clear,
                    audio_alert=audio_alert,
                    music_uri=music_uri,
                    message=message,
                    message_send=message_send,
                    message_delay=message_delay,
                    expect_response=expect_response,
                ),
                "continue_session": continue_session,
            },
        )
        self.bus.emit(message)

        return mycroft_session_id

    def continue_session(
        self,
        dialog: Optional[SessionDialogsType] = None,
        speak: Optional[str] = None,
        speak_wait: bool = True,
        gui: Optional[SessionGuisType] = None,
        gui_clear: GuiClear = GuiClear.AUTO,
        audio_alert: Optional[str] = None,
        music_uri: Optional[str] = None,
        expect_response: bool = False,
        message: Optional[Message] = None,
        message_send: MessageSend = MessageSend.AT_START,
        message_delay: float = 0.0,
        mycroft_session_id: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> Message:
        if mycroft_session_id is None:
            # Use session from latest intent handler
            mycroft_session_id = self._mycroft_session_id

        return Message(
            "mycroft.session.continue",
            data={
                "mycroft_session_id": mycroft_session_id,
                "skill_id": self.skill_id,
                "actions": self._build_actions(
                    dialog=dialog,
                    speak=speak,
                    speak_wait=speak_wait,
                    gui=gui,
                    gui_clear=gui_clear,
                    audio_alert=audio_alert,
                    music_uri=music_uri,
                    message=message,
                    message_send=message_send,
                    message_delay=message_delay,
                    expect_response=expect_response,
                ),
                "state": state,
            },
        )

    def end_session(
        self,
        dialog: Optional[SessionDialogsType] = None,
        speak: Optional[str] = None,
        speak_wait: bool = True,
        gui: Optional[SessionGuisType] = None,
        gui_clear: GuiClear = GuiClear.AUTO,
        audio_alert: Optional[str] = None,
        music_uri: Optional[str] = None,
        message: Optional[Message] = None,
        message_send: MessageSend = MessageSend.AT_START,
        message_delay: float = 0.0,
        mycroft_session_id: Optional[str] = None,
    ) -> Message:
        if mycroft_session_id is None:
            # Use session from latest intent handler
            mycroft_session_id = self._mycroft_session_id

        return Message(
            "mycroft.session.end",
            data={
                "mycroft_session_id": mycroft_session_id,
                "skill_id": self.skill_id,
                "actions": self._build_actions(
                    dialog=dialog,
                    speak=speak,
                    speak_wait=speak_wait,
                    gui=gui,
                    gui_clear=gui_clear,
                    audio_alert=audio_alert,
                    music_uri=music_uri,
                    message=message,
                    message_send=message_send,
                    message_delay=message_delay,
                ),
            },
        )

    def abort_session(self) -> Message:
        message = self.end_session()
        message.data["aborted"] = True
        return message

    def raw_utterance(
        self, utterance: Optional[str], state: Optional[Dict[str, Any]] = None
    ) -> Optional[Message]:
        """Callback when expect_response=True in continue_session"""
        return None

    def __handle_skill_response(self, message: Message):
        """Verifies that raw utterance is for this skill"""
        if (message.data.get("skill_id") == self.skill_id) and (
            message.data.get("mycroft_session_id") == self._mycroft_session_id
        ):
            utterances = message.data.get("utterances", [])
            utterance = utterances[0] if utterances else None
            state = message.data.get("state")
            result_message: Optional[Message] = None
            try:
                self.acknowledge()
                result_message = self.raw_utterance(utterance, state)
            except Exception:
                LOG.exception("Unexpected error in raw_utterance")

            if result_message is None:
                result_message = self.end_session()

            self.bus.emit(result_message)

    def handle_gui_idle(self) -> bool:
        """Allow skill to override idle GUI screen"""
        return False

    def __handle_gui_idle(self, message: Message):
        if message.data.get("skill_id") == self.skill_id:
            handled = False
            try:
                handled = self.handle_gui_idle()
            except Exception:
                LOG.exception("Unexpected error handling GUI idle message")
            finally:
                self.bus.emit(message.response(data={"handled": handled}))
