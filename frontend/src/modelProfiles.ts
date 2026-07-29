export interface ModelProfile {
  id: "qwen-8004" | "minicpm-8005" | "custom";
  name: string;
  endpoint: string;
  note: string;
}

export const MODEL_PROFILES: ModelProfile[] = [
  {
    id: "qwen-8004",
    name: "Qwen3-Omni",
    endpoint: "http://192.168.1.97:8004",
    note: "默认 OpenAI 音频接口",
  },
  {
    id: "minicpm-8005",
    name: "MiniCPM-o-4.5",
    endpoint: "http://192.168.1.97:8005",
    note: "Comni Gateway 音频接口",
  },
  {
    id: "custom",
    name: "自定义",
    endpoint: "",
    note: "自动探测服务协议",
  },
];

export function profileForEndpoint(endpoint: string, defaultEndpoint: string) {
  const normalized = (endpoint || defaultEndpoint).replace(/\/$/, "");
  return MODEL_PROFILES.find(
    (profile) => profile.endpoint && profile.endpoint === normalized,
  ) ?? MODEL_PROFILES[2];
}
