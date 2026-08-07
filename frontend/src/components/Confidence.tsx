import { confidenceClass, percent } from "../format";

export function Confidence({ value }: { value: number | null }) {
  return (
    <span className={`confidence ${confidenceClass(value)}`}>
      {percent(value)}
    </span>
  );
}
