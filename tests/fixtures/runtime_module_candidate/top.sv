module fixture_top (
  input  logic [15:0] bus_i,
  input  logic        select_i,
  output logic [7:0]  low_o,
  output logic [3:0]  high_o
);
  fixture_leaf #(.WIDTH(8)) u_low (
    .data_i   (bus_i[7:0]),
    .enable_i (select_i),
    .data_o   (low_o)
  );

  fixture_leaf #(.WIDTH(4)) u_high (
    .data_i   ({bus_i[15:14], bus_i[9:8]}),
    .enable_i (1'b1),
    .data_o   (high_o)
  );
endmodule
