import { LatestRequest } from "./latestRequest.ts";

export type AbortableRequest = {
  id: number;
  signal: AbortSignal;
};

export class AbortableLatestRequest {
  private readonly requests = new LatestRequest();
  private controller: AbortController | null = null;

  begin(): AbortableRequest {
    this.controller?.abort();
    this.controller = new AbortController();
    return {
      id: this.requests.begin(),
      signal: this.controller.signal,
    };
  }

  isCurrent(id: number): boolean {
    return this.requests.isCurrent(id);
  }

  invalidate(): void {
    this.requests.invalidate();
    this.controller?.abort();
    this.controller = null;
  }

  settle(id: number): void {
    if (!this.requests.isCurrent(id)) return;
    this.controller = null;
  }
}
