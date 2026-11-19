/** Test-only orchestration records, read from the wire fixture through the same strict parsers. Not imported by the app. */
import { HubApiError } from "../api";
import fixture from "./orchestration-wire-v1.json";
import { parseRequestDetail, parseRequestList, type OrchestrationReader } from "../orchestrationApi";

export const fixtureOrchestrationReader: OrchestrationReader = {
  list: async () => parseRequestList(structuredClone(fixture.list)),
  read: async (requestId) => {
    const detail = fixture.details.find(item => item.request.request_id === requestId);
    if (!detail) throw new HubApiError(404, "orchestration_request_not_found");
    return parseRequestDetail(structuredClone(detail), requestId);
  },
};
