export function SignalMark() {
  return (
    <div className="signal-mark" aria-hidden="true">
      {[16, 28, 40, 22, 34, 15].map((height, index) => (
        <span key={index} style={{ height }} />
      ))}
    </div>
  );
}
