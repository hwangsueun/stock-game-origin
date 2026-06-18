import { GameSession } from "../domain/game/GameSession";
import { GamePhase } from "../domain/game/GamePhase";

export class InvestmentTurnService {
  /**
   * 현재 턴 종료
   * - 월급/생활비 처리
   * - 20턴이면 REPAYMENT로 전환
   * - 아니면 다음 턴으로
   */
  endTurn(session: GameSession): void {
    if (session.phase !== GamePhase.INVESTMENT) {
      throw new Error("InvestmentTurnService.endTurn: INVESTMENT phase가 아닙니다");
    }

    const turn = session.investmentTurn;
    const { monthlySalary, livingCost } = session.config;

    // 월급 지급
    session.player.receiveSalary(monthlySalary);
    session.addLog({
      turnIndex: turn,
      logType: "SALARY",
      amount: monthlySalary,
      message: `[월급] +${monthlySalary.toLocaleString()}원`,
    });

    // 생활비 차감
    session.player.payLivingCost(livingCost);
    session.addLog({
      turnIndex: turn,
      logType: "LIVING_COST",
      amount: livingCost,
      message: `[생활비] -${livingCost.toLocaleString()}원`,
    });

    // 턴 전환 (GameSession 내부에서 20턴 체크 후 phase 변경)
    session.endTurn();
  }

  /**
   * 보유 후 턴 종료 (매수/매도 없이 넘김)
   */
  hold(session: GameSession): void {
    if (session.phase !== GamePhase.INVESTMENT) {
      throw new Error("InvestmentTurnService.hold: INVESTMENT phase가 아닙니다");
    }

    session.addLog({
      turnIndex: session.investmentTurn,
      logType: "HOLD",
      message: `[보유] ${session.investmentTurn}턴 — 거래 없이 보유`,
    });

    this.endTurn(session);
  }
}