module example (
  input  logic       clk_i,
  input  logic       rst_ni,
  input  logic       en_i,
  output logic [7:0] count_o
);
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      count_o <= 8'h00;
    end else if (en_i) begin
      count_o <= count_o + 8'd1;
    end
  end
endmodule
