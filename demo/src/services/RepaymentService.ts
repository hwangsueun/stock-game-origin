import { GameSession } from "../domain/game/GameSession";
import { GamePhase } from "../domain/game/GamePhase";
import { RepaymentPolicy } from "./RepaymentPolicy";
import type { RepaymentResult } from "../domain/loan/RepaymentResult";

export class RepaymentService {
  private readonly policy: RepaymentPolicy;

  constructor() {
    this.policy = new RepaymentPolicy();
  }

  /**
   * 상환 처리
   * @param session   현재 게임 세션
   * @param amount    플레이어가 입력한 상환 금액 (0 가능 — DANGER 등급)
   */
  repay(session: GameSession, amount: number): RepaymentResult {
    if (session.phase !== GamePhase.REPAYMENT) {
      throw new Error("RepaymentService.repay: REPAYMENT phase가 아닙니다");
    }
    if (amount < 0) throw new Error("RepaymentService.repay: amount must be >= 0");

    // 현금이 부족하면 가진 만큼만
    const actualAmount = Math.min(amount, session.player.cash, session.loan.remainingDebt);

    // 현금 차감
    if (actualAmount > 0) {
      session.player.deductCash(actualAmount);
    }

    // Loan 상환
    const paid = session.loan.repay(actualAmount);
    const remaining = session.loan.remainingDebt;

    // 등급 계산
    const result = this.policy.calculate({
      initialDebt: session.loan.initialDebt,
      amountPaid: paid,
      remainingDebt: remaining,
    });

    // Trust / Stress 반영
    session.player.trust.apply(result.trustDelta);
    session.player.stress.apply(result.stressDelta);

    // 로그
    session.addLog({
      turnIndex: session.investmentTurn,
      logType: "REPAYMENT",
      amount: paid,
      message: `[상환] ${paid.toLocaleString()}원 납부 — 등급: ${result.grade} | 남은 빚: ${remaining.toLocaleString()}원`,
    });

    // Phase → DEMO_RESULT
    session.finishRepayment();

    return result;
  }

  /** 최대 상환 가능 금액 (현금과 남은 빚 중 작은 것) */
  maxRepayableAmount(session: GameSession): number {
    return Math.min(session.player.cash, session.loan.remainingDebt);
  }
}
