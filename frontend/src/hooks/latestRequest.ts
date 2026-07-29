export class LatestRequest {
  private revision = 0;

  begin(): number {
    this.revision += 1;
    return this.revision;
  }

  invalidate(): void {
    this.revision += 1;
  }

  capture(): number {
    return this.revision;
  }

  isCurrent(revision: number): boolean {
    return revision === this.revision;
  }
}
