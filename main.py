"""Main module of kytos/of_multi_table Kytos Network Application.

This NApp implements Oplenflow multi tables
"""
# pylint: disable=unused-argument, too-many-arguments, too-many-public-methods
# pylint: disable=attribute-defined-outside-init
import pathlib
import time
from typing import Dict, Optional

import httpx
import tenacity
from httpx._exceptions import RequestError
from pydantic import ValidationError
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_fixed)

from kytos.core import KytosNApp, log, rest
from kytos.core.events import KytosEvent
from kytos.core.helpers import listen_to, load_spec, validate_openapi
from kytos.core.rest_api import (HTTPException, JSONResponse, Request,
                                 get_json_or_400)
from kytos.core.retry import before_sleep

from .controllers import PipelineController
from .settings import (BATCH_INTERVAL, BATCH_SIZE, COOKIE_PREFIX,
                       DEFAULT_PIPELINE, FLOW_MANAGER_URL, SUBSCRIBED_NAPPS)


class Main(KytosNApp):
    """Main class of kytos/of_multi_table NApp.

    This class is the entry point for this NApp.
    """

    spec = load_spec(pathlib.Path(__file__).parent / "openapi.yml")

    def setup(self):
        """Replace the '__init__' method for the KytosNApp subclass.

        The setup method is automatically called by the controller when your
        application is loaded.

        So, if you have any setup routine, insert it here.
        """
        self.default_pipeline = DEFAULT_PIPELINE
        self.subscribed_napps = SUBSCRIBED_NAPPS
        self.pipeline_controller = self.get_pipeline_controller()
        self.required_napps = set()
        self.load_pipeline(self.get_enabled_table())

    def execute(self):
        """Execute once when the napp is running."""

    def get_enabled_table(self) -> dict:
        """Get the only enabled table, if exists"""
        pipeline = self.pipeline_controller.get_active_pipeline()
        if pipeline.get("status") in {"enabling", "enabled"}:
            return pipeline
        return self.default_pipeline

    def load_pipeline(self, pipeline: dict):
        """If a pipeline was received, set 'self' variables"""
        found_napps = set()
        content = self.build_content(pipeline)
        enable_napps = self.get_enabled_napps()
        # Find NApps in the pipeline to notify them
        for napp in content:
            if napp in enable_napps:
                found_napps.add(napp)
        self.required_napps = found_napps
        self.start_enabling_pipeline(content)

    def get_enabled_napps(self) -> set:
        """Get the NApps that are enabled and subscribed"""
        enable_napps = set()
        for key in self.controller.napps:
            # Keys look like this: ('kytos', 'of_lldp')
            if key[1] in self.subscribed_napps:
                enable_napps.add(key[1])
        return enable_napps

    def start_enabling_pipeline(self, content: dict):
        """Method to start the process to enable table
        First, send event notifying NApps about their
        new table set up.
        """
        name = "enable_table"
        self.emit_event(name, content)

    def build_content(self, pipeline: dict) -> dict:
        """Build content to be sent through an event"""
        content = {}
        for table in pipeline["multi_table"]:
            table_id = table["table_id"]
            for napp in table["napps_table_groups"]:
                if napp not in content:
                    content[napp] = {}
                for flow_type in table["napps_table_groups"][napp]:
                    content[napp][flow_type] = table_id
        return content

    def emit_event(self, name: str, content: dict = None):
        """Send event"""
        context = "kytos/of_multi_table"
        event_name = f"{context}.{name}"
        event = KytosEvent(name=event_name, content=content)
        self.controller.buffers.app.put(event)

    @listen_to("kytos/(mef_eline|coloring|of_lldp).enable_table")
    def on_enable_table(self, event):
        """Listen for NApps responses"""
        self.handle_enable_table(event)

    def handle_enable_table(self, event):
        """Handle NApps responses from enable_table
        Second, wait for all the napps to respond"""
        napp = event.name.split('/')[1].split('.')[0]
        # Check against the last current table
        self.required_napps.remove(napp)
        if self.required_napps:
            # There are more required napps, 'waiting' responses
            return
        self.get_flows_to_be_installed()

    @retry(
            stop=stop_after_attempt(3),
            wait=wait_fixed(20),
            before_sleep=before_sleep,
            retry=retry_if_exception_type(RequestError))
    def get_installed_flows(self) -> Optional[Dict]:
        """Get flows from flow_manager"""
        command = "v2/stored_flows?state=installed"
        response = httpx.get(f"{FLOW_MANAGER_URL}/{command}")

        if response.status_code // 100 != 2:
            log.error(f"Could not get the flows from flow_mager. Status "
                      f"code {response.status_code}")
            return None
        return response.json()

    def get_flows_to_be_installed(self):
        """Get flows from flow manager so this NApp can modify them
        Third, install the flows with different table_id"""
        pipeline = self.pipeline_controller.get_active_pipeline()
        if not pipeline or pipeline.get("status") == "enabled":
            # Default or enabled pipeline, not need to get flows
            return

        pipeline_id = pipeline['id']
        if pipeline.get("status") == "disabling":
            pipeline = self.default_pipeline

        try:
            flows_by_swich = self.get_installed_flows()
        except tenacity.RetryError as err:
            raise HTTPException(424, "It couldn't get stored_flows") from err

        if flows_by_swich is None:
            return

        set_up = self.build_content(pipeline)
        delete_flows = {}
        install_flows = {}
        for switch in flows_by_swich:
            delete_flows[switch] = []
            install_flows[switch] = []
            for flow in flows_by_swich[switch]:
                owner = flow["flow"].get("owner")
                if owner not in set_up:
                    continue
                expected_table_id = set_up[owner][flow["flow"]["table_group"]]
                # if table_id needs to change
                if expected_table_id != flow["flow"]["table_id"]:
                    # Get key-value from flow to be sent to flow_manager
                    delete = {
                        'cookie': flow["flow"].get('cookie'),
                        'cookie_mask': int(0xFFFFFFFFFFFFFFFF),
                    }
                    if flow["flow"].get('match'):
                        delete['match'] = flow["flow"].get('match')
                    delete_flows[switch].append(delete.copy())
                    # Change table_id before being added
                    flow["flow"].update({"table_id": expected_table_id})
                    install_flows[switch].append(flow["flow"].copy())
        self.send_flows(delete_flows, 'delete')
        self.send_flows(install_flows, 'install')

        if pipeline.get("status") is None:
            # Changing to default pipeline. Miss flow entries are not needed
            self.delete_miss_flows()
            self.pipeline_controller.disabled_pipeline(pipeline_id)
        else:
            self.install_miss_flows(pipeline)
            self.pipeline_controller.enabled_pipeline(pipeline_id)

    def install_miss_flows(self, pipeline: dict):
        """Install miss flow entry to a switch"""
        install_flows = {}
        for switch in self.controller.switches:
            install_flows[switch] = []
            cookie = self.get_cookie(switch)
            for table in pipeline["multi_table"]:
                miss_flow = table.get("table_miss_flow")
                if miss_flow:
                    flow = {
                        'priority': miss_flow.get('priority', 0),
                        'cookie': cookie,
                        'owner': 'of_multi_table',
                        'table_group': 'base',
                        'table_id': table['table_id'],
                    }
                    if miss_flow.get('match'):
                        flow['match'] = miss_flow.get('match')
                    instruction = miss_flow.get('instructions')
                    if instruction and instruction[0]:
                        flow['instructions'] = miss_flow.get('instructions')
                    install_flows[switch].append(flow)
        self.send_flows(install_flows, 'install')

    def delete_miss_flows(self):
        """Delete miss flows, aka. of_multi_table flows.
        This method is called when returning to default pipeline."""
        flow = {
            "cookie": int(COOKIE_PREFIX << 56),
            "cookie_mask": int(0xFF00000000000000)
        }
        delete_flows = {}
        for switch in self.controller.switches:
            delete_flows[switch] = [flow]
        self.send_flows(delete_flows, 'delete')

    def send_flows(self, flows: Dict, action: str, force: bool = True):
        """Send flows to flow_manager through event"""
        offset = BATCH_SIZE or None
        while flows:
            switch = list(flows.keys())
            for dpid in switch:
                if len(flows[dpid]) == 0:
                    del flows[dpid]
                    continue
                name = f"kytos.flow_manager.flows.{action}"
                content = {
                    'dpid': dpid,
                    'flow_dict': {"flows": flows[dpid][:offset]},
                    'force': force
                }
                event = KytosEvent(name=name, content=content)
                self.controller.buffers.app.put(event)
                if offset is None or offset >= len(flows[dpid]):
                    del flows[dpid]
                    continue
                flows[dpid] = flows[dpid][offset:]
            time.sleep(BATCH_INTERVAL)

    @staticmethod
    def get_pipeline_controller():
        """Get PipelineController"""
        return PipelineController()

    @rest("/v1/pipeline", methods=["POST"])
    @validate_openapi(spec)
    def add_pipeline(self, request: Request) -> JSONResponse:
        """Add pipeline"""
        data = get_json_or_400(request, self.controller.loop)
        log.debug(f"add_pipeline /v1/pipeline content: {data}")
        try:
            _id = self.pipeline_controller.insert_pipeline(data)
        except ValidationError as err:
            msg = self.error_msg(err.errors())
            log.debug(f"add_pipeline result {msg} 400")
            raise HTTPException(400, detail=msg) from err
        msg = {"id": _id}
        log.debug(f"add_pipeline result {msg} 201")
        return JSONResponse({"id": _id}, status_code=201)

    @rest("/v1/pipeline", methods=["GET"])
    def list_pipelines(self, request: Request) -> JSONResponse:
        """List pipelines"""
        log.debug("list_pipelines /v1/pipeline")
        status = request.query_params.get("status", None)
        pipelines = self.pipeline_controller.get_pipelines(status)
        return JSONResponse(pipelines)

    @rest("/v1/pipeline/{pipeline_id}", methods=["GET"])
    def get_pipeline(self, request: Request) -> JSONResponse:
        """Get pipeline by pipeline_id"""
        pipeline_id = request.path_params["pipeline_id"]
        log.debug(f"get_pipeline /v1/pipeline/{pipeline_id}")
        pipeline = self.pipeline_controller.get_pipeline(pipeline_id)
        if not pipeline:
            msg = f"pipeline_id {pipeline_id} not found"
            log.debug(f"get_pipeline result {msg} 404")
            raise HTTPException(404, detail=msg)
        return JSONResponse(pipeline)

    @rest("/v1/pipeline/{pipeline_id}", methods=["DELETE"])
    def delete_pipeline(self, request: Request) -> JSONResponse:
        """Delete pipeline by pipeline_id"""
        pipeline_id = request.path_params["pipeline_id"]
        log.debug(f"delete_pipeline /v1/pipeline/{pipeline_id}")
        pipeline = self.pipeline_controller.get_pipeline(pipeline_id)
        if pipeline is None:
            msg = f"pipeline_id {pipeline_id} not found"
            log.debug(f"delete_pipeline result {msg} 404")
            raise HTTPException(404, detail=msg)
        if pipeline["status"] in {"enabled", "enabling", "disabling"}:
            msg = "Only disabled pipelines are allowed to be delete"
            log.debug(f"delete_pipeline result {msg} 409")
            raise HTTPException(409, detail=msg)
        self.pipeline_controller.delete_pipeline(pipeline_id)
        msg = f"Pipeline {pipeline_id} deleted successfully"
        log.debug(f"delete_pipeline result {msg} 200")
        return JSONResponse(msg)

    @rest("/v1/pipeline/{pipeline_id}/enable", methods=["POST"])
    def enable_pipeline(self, request: Request) -> JSONResponse:
        """Enable pipeline"""
        pipeline_id = request.path_params["pipeline_id"]
        log.debug(f"enable_pipeline /v1/pipeline/{pipeline_id}/enable")
        pipeline = self.pipeline_controller.get_active_pipeline()
        status = pipeline.get("status")
        id_ = pipeline.get("id")
        if pipeline and (id_ != pipeline_id or status == "disabling"):
            msg = f"Other pipeline {id_} is {status}"
            log.debug(f"enable_pipeline result {msg} 409")
            raise HTTPException(409, detail=msg)

        if status != "enabled":
            pipeline = self.pipeline_controller.enabling_pipeline(pipeline_id)
            if not pipeline:
                msg = f"Pipeline {pipeline_id} not found"
                log.debug(f"enable_pipeline result {msg} 404")
                raise HTTPException(404, detail=msg)
            self.load_pipeline(pipeline)
        msg = f"Pipeline {pipeline_id} enabling"
        log.debug(f"enable_pipeline result {msg} 200")
        return JSONResponse(msg)

    @rest("/v1/pipeline/{pipeline_id}/disable", methods=["POST"])
    def disable_pipeline(self, request: Request) -> JSONResponse:
        """Disable pipeline"""
        pipeline_id = request.path_params["pipeline_id"]
        log.debug(f"disable_pipeline /v1/pipeline/{pipeline_id}/disable")
        pipeline = self.pipeline_controller.get_active_pipeline()
        status = pipeline.get("status")
        id_ = pipeline.get("id")
        if pipeline and (id_ != pipeline_id or status == "enabling"):
            msg = f"Other pipeline {id_} is {status}"
            log.debug(f"disable_pipeline result {msg} 409")
            raise HTTPException(409, detail=msg)

        if status != "disabled":
            pipeline = self.pipeline_controller.disabling_pipeline(pipeline_id)
            if not pipeline:
                msg = f"Pipeline {pipeline_id} not found"
                log.debug(f"disable_pipeline result {msg} 404")
                raise HTTPException(404, detail=msg)
            self.load_pipeline(self.default_pipeline)
        msg = f"Pipeline {pipeline_id} disabled"
        log.debug(f"disable_pipeline result {msg} 200")
        return JSONResponse(msg)

    @listen_to("kytos/flow_manager.flow.added")
    def on_flow_mod_added(self, event):
        """Looking for recently added flows"""
        self.handle_flow_mod_added(event)

    def handle_flow_mod_added(self, event):
        """Handle recently added flows"""

    @listen_to("kytos/flow_manager.flow.error")
    def on_flow_mod_error(self, event):
        """Handle flow mod errors"""
        self.handle_flow_mod_error(event)

    def handle_flow_mod_error(self, event):
        """Handle flow mod errors"""

    @staticmethod
    def get_cookie(switch_dpid) -> int:
        """Return the cookie integer given a dpid."""
        dpid = int(switch_dpid.replace(":", ""), 16)
        return (0x00FFFFFFFFFFFFFF & dpid) | (COOKIE_PREFIX << 56)

    @staticmethod
    def error_msg(error_list: list) -> str:
        """Return a more request friendly error message from ValidationError"""
        msg = ""
        for err in error_list:
            for value in err['loc']:
                msg += str(value) + ", "
            msg = msg[:-2]
            msg += ": " + err["msg"] + "; "
        return msg[:-2]

    def shutdown(self):
        """Run when your NApp is unloaded.

        If you have some cleanup procedure, insert it here.
        """
