import { Stress } from "./Stress";
import { Trust } from "./Trust";

export class Player {
  private _cash: number;
  readonly stress: Stress;
  readonly trust: Trust;

  constructor(params: {
    initialCash: number;
    initialStress: number;
    initialTrust: number;
  }) {
    if (params.initialCash < 0) throw new Error("Player: initialCash must be >= 0");
    this._cash = params.initialCash;
    this.stress = new Stress(params.initialStress);
    this.trust = new Trust(params.initialTrust);
  }

  get cash(): number {
    return this._cash;
  }

  /** 월급 등 수입 */
  receiveSalary(amount: number): void {
    if (amount < 0) throw new Error("Player.receiveSalary: amount must be >= 0");
    this._cash += amount;
  }

  /** 생활비 등 지출 */
  payLivingCost(amount: number): void {
    if (amount < 0) throw new Error("Player.payLivingCost: amount must be >= 0");
    this._cash -= amount;
    // 현금이 마이너스가 될 수 있음 — 판단은 상위에서
  }

  /** 매수/상환 등 직접 차감 */
  deductCash(amount: number): void {
    if (amount < 0) throw new Error("Player.deductCash: amount must be >= 0");
    if (this._cash < amount) throw new Error("Player.deductCash: insufficient cash");
    this._cash -= amount;
  }

  /** 매도 수익 등 직접 증가 */
  addCash(amount: number): void {
    if (amount < 0) throw new Error("Player.addCash: amount must be >= 0");
    this._cash += amount;
  }

  hasCash(amount: number): boolean {
    return this._cash >= amount;
  }
}