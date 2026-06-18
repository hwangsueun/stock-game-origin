import { GamePhase } from "./GamePhase";
import type { GameCalendar } from "./GameCalendar";
import { Player } from "../player/Player";
import { Market } from "../market/Market";
import { Portfolio } from "../portfolio/Portfolio";
import { Loan } from "../loan/Loan";
import type { GameLog } from "../log/GameLog";
import { createLog } from "../log/GameLog";

export type GameConfig = {
  configId: string;
  initialCash: number;
  initialDebt: number;
  monthlySalary: number;
  livingCost: number;
  initialStress: number;
  initialTrust: number;
  maxInvestmentTurn: number;
  repaymentDueTurn: number;
}

export class GameSession {
  private _phase: GamePhase;
  private _investmentTurn: number;   // 0 = 시작 전, 1~20 = 투자 중
  readonly player: Player;
  readonly market: Market;
  readonly portfolio: Portfolio;
  readonly loan: Loan;
  readonly config: GameConfig;
  private readonly _logs: GameLog[];

  constructor(params: { config: GameConfig; market: Market }) {
    const { config, market } = params;
    this._phase = GamePhase.START_STORY;
    this._investmentTurn = 0;
    this.config = config;
    this.market = market;
    this.player = new Player({
      initialCash: config.initialCash,
      initialStress: config.initialStress,
      initialTrust: config.initialTrust,
    });
    this.portfolio = new Portfolio();
    this.loan = new Loan(config.initialDebt);
    this._logs = [];

    this.addLog({
      turnIndex: 0,
      logType: "PHASE_CHANGE",
      message: "게임이 시작되었습니다.",
    });
  }

  // ─── Getters ───────────────────────────────────────────

  get phase(): GamePhase {
    return this._phase;
  }

  get investmentTurn(): number {
    return this._investmentTurn;
  }

  get currentTurnIndex(): number {
    return this._investmentTurn;
  }

  get logs(): GameLog[] {
    return [...this._logs];
  }

  get isLastTurn(): boolean {
    return this._investmentTurn === this.config.maxInvestmentTurn;
  }

  // ─── Phase 전환 ─────────────────────────────────────────

  /** START_STORY → INVESTMENT */
  startInvestment(): void {
    if (this._phase !== GamePhase.START_STORY) {
      throw new Error("GameSession.startInvestment: phase must be START_STORY");
    }
    this._phase = GamePhase.INVESTMENT;
    this._investmentTurn = 1;

    this.addLog({
      turnIndex: this._investmentTurn,
      logType: "PHASE_CHANGE",
      message: `투자 1턴이 시작되었습니다. (${this.market.calendar.getDate(1)})`,
    });
  }

  /** 턴 종료 — 다음 턴으로 이동하거나 REPAYMENT로 전환 */
  endTurn(): void {
    if (this._phase !== GamePhase.INVESTMENT) {
      throw new Error("GameSession.endTurn: phase must be INVESTMENT");
    }
    if (this._investmentTurn >= this.config.maxInvestmentTurn) {
      // 20턴 완료 — turn 번호 유지, phase만 변경
      this._phase = GamePhase.REPAYMENT;
      this.addLog({
        turnIndex: this._investmentTurn,
        logType: "PHASE_CHANGE",
        message: `20턴 투자가 완료되었습니다. 상환 단계로 진입합니다.`,
      });
    } else {
      this._investmentTurn += 1;
      this.addLog({
        turnIndex: this._investmentTurn,
        logType: "PHASE_CHANGE",
        message: `${this._investmentTurn}턴이 시작되었습니다. (${this.market.calendar.getDate(this._investmentTurn)})`,
      });
    }
  }

  /** REPAYMENT → DEMO_RESULT */
  finishRepayment(): void {
    if (this._phase !== GamePhase.REPAYMENT) {
      throw new Error("GameSession.finishRepayment: phase must be REPAYMENT");
    }
    this._phase = GamePhase.DEMO_RESULT;
    this.addLog({
      turnIndex: this._investmentTurn,
      logType: "PHASE_CHANGE",
      message: "상환이 완료되었습니다. 결과를 확인하세요.",
    });
  }

  // ─── 로그 ────────────────────────────────────────────────

  addLog(params: Omit<GameLog, "id" | "timestamp">): void {
    this._logs.push(createLog(params));
  }
}
