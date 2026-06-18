export type GameLogType =
  | "BUY"
  | "SELL"
  | "HOLD"
  | "SALARY"
  | "LIVING_COST"
  | "REPAYMENT"
  | "PHASE_CHANGE"
  | "INFO";

export type GameLog = {
  id: string;             // crypto.randomUUID() or nanoid
  turnIndex: number;
  logType: GameLogType;
  message: string;
  amount?: number;
  assetId?: string;
  timestamp: number;      // Date.now()
}

export function createLog(
  params: Omit<GameLog, "id" | "timestamp">
): GameLog {
  return {
    ...params,
    id: crypto.randomUUID(),
    timestamp: Date.now(),
  };
}
