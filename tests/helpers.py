""""""
from asyncio import Future, run_coroutine_threadsafe
from contextlib import contextmanager, asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import NewType, Type, TypeAlias, TypeVar, Generic

from freezegun.api import freeze_time, FrozenDateTimeFactory, StepTickTimeFactory

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import MerossApi, MerossDevice, emulator as em
from custom_components.meross_lan.emulator import MerossEmulator
from custom_components.meross_lan.merossclient import const as mc
from custom_components.meross_lan.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_PAYLOAD,
    CONF_POLLING_PERIOD,
    DOMAIN,
)

from .const import (
    EMULATOR_TRACES_MAP,
    EMULATOR_TRACES_PATH,
    MOCK_DEVICE_UUID,
    MOCK_HTTP_RESPONSE_DELAY,
    MOCK_KEY,
    MOCK_POLLING_PERIOD,
)


MerossDeviceType = TypeVar('MerossDeviceType', bound=MerossDevice)

def build_emulator(model: str) -> MerossEmulator:
    # Watchout: this call will not use the uuid and key set
    # in the filename, just DEFAULT_UUID and DEFAULT_KEY
    return em.build_emulator(
        EMULATOR_TRACES_PATH + EMULATOR_TRACES_MAP[model], MOCK_DEVICE_UUID, MOCK_KEY
    )


def build_emulator_config_entry(emulator: MerossEmulator):

    device_uuid = emulator.descriptor.uuid
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DEVICE_ID: device_uuid,
            CONF_HOST: device_uuid,
            CONF_KEY: emulator.key,
            CONF_PAYLOAD: {
                mc.KEY_ALL: deepcopy(emulator.descriptor.all),
                mc.KEY_ABILITY: deepcopy(emulator.descriptor.ability),
            },
            CONF_POLLING_PERIOD: MOCK_POLLING_PERIOD,
        },
        unique_id=device_uuid,
        version=1,
    )


@contextmanager
def emulator_mock(
    emulator_: MerossEmulator | str,
    aioclient_mock: 'AiohttpClientMocker',
    frozen_time: FrozenDateTimeFactory | StepTickTimeFactory | None = None,
):
    """
        This context provides an emulator working on HTTP  by leveraging
        the aioclient_mock.
        This is a basic mock which is not polluting HA
    """
    try:
        if isinstance(emulator_, str):
            emulator_ = build_emulator(emulator_)

        async def _handle_http_request(method, url, data):
            response = emulator_.handle(data) # pylint: disable=no-member
            if frozen_time is not None:
                frozen_time.tick(timedelta(seconds=MOCK_HTTP_RESPONSE_DELAY))  # emulate http roundtrip time
            return AiohttpClientMockResponse(method, url, json=response)

        # we'll use the uuid so we can mock multiple at the same time
        # and the aioclient_mock will route accordingly
        aioclient_mock.post(
            f"http://{emulator_.descriptor.uuid}/config", # pylint: disable=no-member
            side_effect=_handle_http_request,
        )

        yield emulator_

    finally:
        # remove the mock from aioclient
        aioclient_mock.clear_requests()


class DeviceContext(Generic[MerossDeviceType]):
    hass: HomeAssistant
    config_entry: MockConfigEntry
    api: MerossApi
    emulator: MerossEmulator
    device: MerossDeviceType | None
    time: FrozenDateTimeFactory | StepTickTimeFactory
    _warp_task: Future | None = None
    _warp_run: bool

    async def perform_coldstart(self):
        """
        to be called after setting up a device (context) to actually
        execute the cold-start polling sequence.
        After this the device should be online and all the polling
        namespaces done
        """
        assert self.device is not None
        async_fire_time_changed_exact(self.hass)
        await self.hass.async_block_till_done()
        assert self.device.online

    async def async_load_config_entry(self):
        assert self.device is None
        hass = self.hass
        assert await hass.config_entries.async_setup(self.config_entry.entry_id)
        await hass.async_block_till_done()
        self.api = hass.data[DOMAIN]
        self.device = self.api.devices[self.config_entry.unique_id]
        assert not self.device.online

    async def async_unload_config_entry(self):
        assert self.device is not None
        hass = self.hass
        assert await hass.config_entries.async_unload(self.config_entry.entry_id)
        await hass.async_block_till_done()
        assert self.config_entry.unique_id not in hass.data[DOMAIN].devices
        self.device = None # discard our local reference so the device gets destroyed

    async def async_enable_entity(self, entity_id):
        # entity enable will reload the config_entry
        # by firing a trigger event which will the be collected by
        # config_entries
        # so we have to recover the right instances
        ent_reg = entity_registry.async_get(self.hass)
        ent_reg.async_update_entity(entity_id, disabled_by=None)
        # fire the entity registry changed
        await self.hass.async_block_till_done()
        # perform the reload task after RELOAD_AFTER_UPDATE_DELAY
        await self.async_tick(timedelta(seconds=config_entries.RELOAD_AFTER_UPDATE_DELAY))
        # gather the new instances
        self.api = self.hass.data[DOMAIN]
        self.device = self.api.devices[self.config_entry.unique_id]
        # online the device
        await self.perform_coldstart()

    async def async_tick(self, tick: timedelta):
        self.time.tick(tick)
        async_fire_time_changed_exact(self.hass)
        await self.hass.async_block_till_done()

    async def async_move_to(self, target_datetime: datetime):
        self.time.move_to(target_datetime)
        async_fire_time_changed_exact(self.hass)
        await self.hass.async_block_till_done()

    async def async_warp(
        self,
        timeout: float | int | timedelta | datetime,
        tick: float | int | timedelta = 1
    ):
        if not isinstance(timeout, datetime):
            if isinstance(timeout, timedelta):
                timeout = self.time() + timeout
            else:
                timeout = self.time() + timedelta(seconds=timeout)
        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        while self.time() < timeout:
            await self.async_tick(tick)

    def warp(self, tick: float | int | timedelta = .5):
        """
            starts an asynchronous task which manipulates our
            freze_time so the time passes and get advanced to
            time.time() + timeout.
            While passing it tries to perform HA events rollout
            every tick seconds
        """
        assert self._warp_task is None

        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        def _warp():
            try:
                while self._warp_run:
                    run_coroutine_threadsafe(self.async_tick(tick), self.hass.loop)
            except Exception as error:
                pass

        self._warp_run = True
        self._warp_task = self.hass.async_add_executor_job(_warp)

    async def async_stopwarp(self):
        assert self._warp_task
        self._warp_run = False
        await self._warp_task
        self._warp_task = None


@asynccontextmanager
async def devicecontext(
    emulator: MerossEmulator | str,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    time_to_freeze=None,
):
    """
        This is a 'full featured' context providing an emulator and setting it
        up as a configured device in HA
        It also provides timefreezing
    """
    with freeze_time(time_to_freeze) as frozen_time:
        with emulator_mock(emulator, aioclient_mock, frozen_time) as emulator:

            context = DeviceContext()
            context.hass = hass
            context.time = frozen_time
            context.emulator = emulator
            context.config_entry = build_emulator_config_entry(emulator)
            context.config_entry.add_to_hass(hass)
            context.device = None
            await context.async_load_config_entry()
            try:
                yield context
            finally:
                await context.async_unload_config_entry()
