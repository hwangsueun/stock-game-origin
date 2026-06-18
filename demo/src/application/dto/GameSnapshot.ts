import { GamePhase } from "../../domain/game/GamePhase";
import type { GameLog } from "../../domain/log/GameLog";
import type { AssetValuation } from "../../services/PortfolioValuationService";

export type MarketRow = {
  assetId: string;
  name: string;
  assetType: string;
  closePrice: number;
  changeRate: number;   // 소수점 (0.032 = +3.2%)
  currency: string;
}

export type GameSnapshot = {
  // 게임 진행
  phase: GamePhase;
  investmentTurn: number;
  currentDate: string;
  maxTurn: number;

  // 플레이어
  cash: number;
  stress: number;
  trust: number;

  // 대출
  initialDebt: number;
  remainingDebt: number;

  // 시장
  marketRows: MarketRow[];

  // 포트폴리오 평가
  investmentValue: number;
  totalAssets: number;
  netAssets: number;
  holdings: AssetValuation[];

  // 로그
  logs: GameLog[];
}
