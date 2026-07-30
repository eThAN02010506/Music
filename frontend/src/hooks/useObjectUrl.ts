import { useCallback, useEffect, useRef, useState } from "react";

export interface ObjectUrlApi {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
}

export class ObjectUrlSlot {
  private current = "";
  private readonly urlApi: ObjectUrlApi;

  constructor(urlApi: ObjectUrlApi) {
    this.urlApi = urlApi;
  }

  replace(blob: Blob | null): string {
    const previous = this.current;
    this.current = blob ? this.urlApi.createObjectURL(blob) : "";
    if (previous) this.urlApi.revokeObjectURL(previous);
    return this.current;
  }

  dispose(): void {
    if (!this.current) return;
    const previous = this.current;
    this.current = "";
    this.urlApi.revokeObjectURL(previous);
  }
}

export function useObjectUrl() {
  const slotRef = useRef<ObjectUrlSlot | null>(null);
  if (slotRef.current === null) slotRef.current = new ObjectUrlSlot(URL);
  const [url, setUrl] = useState("");

  const setBlob = useCallback((blob: Blob | null) => {
    setUrl(slotRef.current!.replace(blob));
  }, []);

  const clear = useCallback(() => {
    slotRef.current!.dispose();
    setUrl("");
  }, []);

  useEffect(() => () => {
    slotRef.current?.dispose();
  }, []);

  return { url, setBlob, clear };
}
