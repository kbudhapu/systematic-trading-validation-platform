/** Active trading environment label shared by snapshot and realtime filters. */
export function getTradingEnvironment(): string {
  return (
    process.env.NEXT_PUBLIC_TRADING_ENVIRONMENT?.trim() ||
    process.env.TRADING_ENVIRONMENT?.trim() ||
    "production"
  );
}
