export class Trust {
  private _value: number;

  static readonly MIN = 0;
  static readonly MAX = 100;

  constructor(initial: number) {
    this._value = Trust.clamp(initial);
  }

  get value(): number {
    return this._value;
  }

  increase(amount: number): void {
    if (amount < 0) throw new Error("Trust.increase: amount must be >= 0");
    this._value = Trust.clamp(this._value + amount);
  }

  decrease(amount: number): void {
    if (amount < 0) throw new Error("Trust.decrease: amount must be >= 0");
    this._value = Trust.clamp(this._value - amount);
  }

  apply(delta: number): void {
    this._value = Trust.clamp(this._value + delta);
  }

  isMin(): boolean {
    return this._value <= Trust.MIN;
  }

  private static clamp(v: number): number {
    return Math.max(Trust.MIN, Math.min(Trust.MAX, Math.round(v)));
  }
}