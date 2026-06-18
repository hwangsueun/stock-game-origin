export class Loan {
  private readonly _initialDebt: number;
  private _remainingDebt: number;

  constructor(initialDebt: number) {
    if (initialDebt < 0) throw new Error("Loan: initialDebt must be >= 0");
    this._initialDebt = initialDebt;
    this._remainingDebt = initialDebt;
  }

  get initialDebt(): number {
    return this._initialDebt;
  }

  get remainingDebt(): number {
    return this._remainingDebt;
  }

  isFullyRepaid(): boolean {
    return this._remainingDebt <= 0;
  }

  /** 상환 처리 — 실제 납부 금액 반환 */
  repay(amount: number): number {
    if (amount < 0) throw new Error("Loan.repay: amount must be >= 0");
    const actual = Math.min(amount, this._remainingDebt);
    this._remainingDebt -= actual;
    return actual;
  }

  /** 상환 비율 계산 (0.0 ~ 1.0) */
  repaymentRate(amountPaid: number): number {
    if (this._initialDebt === 0) return 1;
    return Math.min(amountPaid / this._initialDebt, 1);
  }
}